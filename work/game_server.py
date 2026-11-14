# -*- coding: utf-8 -*-
# 妖怪正传 私服服务器 —— 参照 lunar-tear 架构
# 由三部分构成:  Asset CDN + HTTP(auth/config) + Game Socket server
# 协议(已从源码摸清): 消息 = base64(JSON); 帧 = [长度][msgid][seqid][json]; 战斗客户端自算。
import os, io, sys, json, base64, struct, socket, threading, time, hashlib, mimetypes, contextvars, traceback
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 标准输出: 统一成 utf-8 + **自建 stdout 落盘 (tee)** ----
# ★v32 为什么由服务端自己写日志, 而不是让启动脚本 Start-Process -RedirectStandardOutput:
#   ① 旧文档的 `python ... > game_server.log` 会**每次启动截断**日志 (坑点 #31);
#   ② 用启动脚本的重定向会把父 shell 的句柄带进子进程 —— 实测会让"一键启动"在外层
#      管道里挂住不返回 (server 起来了, 脚本却像卡死);
#   ③ 放进程内, 那么**不管谁怎么启动**(脚本/手工/计划任务)日志都一定在 work/logs/server-out.log。
#   用追加模式 -> 历史不丢; 超过 ROLL 字节自动轮转一份 .1。
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _setup_std_streams():
    for _n in ('stdout', 'stderr'):
        _s = getattr(sys, _n, None)
        if _s is not None and hasattr(_s, 'buffer'):
            try:
                setattr(sys, _n, io.TextIOWrapper(_s.buffer, encoding='utf-8', errors='replace'))
            except Exception:
                pass

class _Tee:
    """把写入同时送到多个流 (控制台 + 日志文件); 任一目标出错都不影响另一个。

    ★★2026-10-02: **必须加锁** —— 修前是裸的 `w.write(s)`。服务端是多线程的
      (`ThreadingHTTPServer` + 每连接一个线程), 两个线程同时 `print()` 时文本层的
      编码/缓冲会交错 ⇒ 日志文件里出现**半个多字节字符**(一个 UTF-8 序列被从中间切开)。
      失败形态**不是崩溃而是间歇性**: `_check_encoding.py` 会随机判红
      (本轮回归网实测"跑两次、一次 1 失败一次 0 失败"), 而排查时第一反应会去怀疑
      自己刚改的代码。加锁的代价可以忽略(写日志本来就要 IO), 换来的是
      "日志永远是整行、永远是合法 UTF-8"。
    """
    def __init__(self, *streams):
        self._ss = [s for s in streams if s is not None]
        self._lk = threading.Lock()

    def write(self, s):
        with self._lk:
            for w in self._ss:
                try:
                    w.write(s)
                except Exception:
                    pass
        return len(s)

    def flush(self):
        with self._lk:
            for w in self._ss:
                try:
                    w.flush()
                except Exception:
                    pass

    def isatty(self):
        return False

def _open_std_log(name):
    _d = os.path.join(_BASE_DIR, 'logs')
    try:
        os.makedirs(_d, exist_ok=True)
        _p = os.path.join(_d, name)
        try:
            if os.path.getsize(_p) > 8 * 1024 * 1024:      # 8MB 轮转
                _old = _p + '.1'
                if os.path.exists(_old):
                    os.remove(_old)
                os.replace(_p, _old)
        except Exception:
            pass
        return open(_p, 'a', encoding='utf-8', buffering=1)
    except Exception:
        return None

_setup_std_streams()
try:
    sys.stdout = _Tee(sys.stdout, _open_std_log('server-out.log'))
    sys.stderr = _Tee(sys.stderr, _open_std_log('server-err.log'))
except Exception:
    pass

# ★★2026-10-11（G1 真机验收前置修复）：让**正在跑的服务端进程**里 `import game_server`
#   拿回同一个模块对象，而不是第二份。
#   缺陷：本文件在服务端是 `python game_server.py` 起的 ⇒ 模块名是 `__main__`；
#     而 `handlers_sociaty.py` 里 4 处 `import game_server as _gs`（1134/1152/1154/1159 推送）
#     会**把本文件再执行一遍**，造出一个**第二份模块对象** —— 它的 `LIVE_CONNS` 是空字典
#     ⇒ `_gs.push_account(...)` 永远一条都发不出去。
#   实测（隔离副本 + 两条 LE 连接）：`push_account` 里 `REGISTRY.online` 有连接 fd，
#     但那一份的 `LIVE_CONNS` 为空 ⇒ 帧全部被 `continue` 掉；同进程 `__main__` 那份是有连接的。
#     用户侧表现就是"推送全靠对方刷新面板"（HANDOFF 里 1134/1141 那条欠债）。
#     管理端没这个问题：它在 `__main__` 段里**注入函数对象**
#     （`admin_backend.configure(push_all=push_all, push_account=push_account, ...)`）。
#   ⚠ 测试进程只 `import game_server`（不跑 `__main__` 段），这条 shim 不生效 ⇒ 回归网不受影响。
if __name__ == '__main__':
    sys.modules['game_server'] = sys.modules['__main__']

# ---- 模块化拆分 (方向: game_server -> config_data/state/handlers_equip; handlers_equip -> state/config_data) ----
# state.py        : 玩家可变状态 EQ_STATE + 常量 + JSON 持久化 (load_state/save_state)
# config_data.py  : 静态响应数据常量 (RESPONSES/resp_for/_mon 等)
# handlers_equip.py: 6004/6005/6006 装备处理器 + extract_body_json
# hotupdate.py    : 客户端自带热更通道 (b) 的清单生成 + 资源下发 (ygzz\hotfix_src\)
import hotupdate
import accounts
# ★2026-10-12 (多人单服): 逐条响应诊断 + 登录鉴权。
#   两者都**不 import game_server**（否则会在这里再执行一遍本文件的模块级代码，
#   实测后果是第二份 `LIVE_CONNS` = 推送永远发不出去，见 :86 那段注释）。
import ygzz_diag
import ygzz_auth
from state import *
from config_data import *
from handlers_equip import *
from handlers_draw import *
from handlers_bag import *
from handlers_cult import *
from handlers_sys import *
from handlers_comment import (handle_comment_info, handle_comment_send, handle_comment_praise,
                              handle_rank_send_attr, handle_ranking_praise, handle_war_verification)
from handlers_batch import (handle_push_shop, handle_aggress_wave, handle_aggress_formation,
                            handle_point_update, handle_share, handle_monster_damage,
                            handle_select_language, handle_limitgift_receive,
                            handle_achievement_detail, handle_limitgift_list,
                            handle_ad_notice, handle_fund_act_list, handle_activity_notice)
from handlers_game import *
# ★2026-10-02 (审计 §12.1 / §13 排名 8): 成就 1042/1044 + 小红点链路 105/8082/1053。
#   ⚠ 顺序: 必须放在 handlers_game 之后 —— handlers_game 里的 `handle_task_info`(旧 1042 用的那份)
#     是**空桩级**实现, 本行在它之后导入, 保证 1042 路由到真正的新实现。
#   ⚠ 本模块**不定义** handle_point_update(1054 仍归 handlers_batch.py:114, 别在这里重定义)。
from handlers_achievement import handle_achievement_info, handle_achievement_get
from handlers_point import (handle_point_request, handle_attention_push,
                           push_reddots as _push_reddots)
from handlers_skill import (handle_skill_chip_compose, handle_skill_study,
                            handle_skill_intensify, handle_skill_replace)
from handlers_task import handle_daily_task, handle_daily_task_reward, handle_liveness_reward
# ★2026-10-11（A0 第 2 批）: 在线奖励（1302~1304）+ 限时新手任务剩余时间（4070）。
#   这两个系统是 target client新增的，other version 里连 msgid 都不存在（见模块头注释）。
from handlers_onlinereward import (handle_online_reward_info, handle_online_reward_refresh,
                                   handle_online_reward_draw, handle_limited_time_new_task_time)
from handlers_pvp import (handle_pvp_get_bat_arr, handle_pvp_save_bat_arr, handle_pvp_unlock_map,
                          handle_pvp_receive, handle_pvp_one_key_receive, handle_pvp_enter,
                          handle_pvp_war_result, handle_pvp_match, handle_pvp_get_box_rewards,
                          handle_pvp_receive_box)
from handlers_sociaty import (handle_sociaty_info, handle_sociaty_list,
                              handle_sociaty_create, handle_sociaty_donate,
                              handle_sociaty_join_list, handle_sociaty_apply_join,
handle_sociaty_apply_join_req,
                              handle_sociaty_kick_out, handle_sociaty_notice,
                              handle_sociaty_quit, handle_sociaty_post_change,
                              handle_sociaty_join, handle_sociaty_join_type,
                              handle_sociaty_data_payload, handle_sociaty_week_task,
                              handle_sociaty_task_reward, handle_sociaty_liveness_reward,
                              handle_sociaty_shop_info, handle_sociaty_shop_buy,
                              handle_sociaty_dismiss, handle_sociaty_sync_info,
                              handle_sociaty_shop_upgrade, handle_sociaty_rank_info,
                              handle_sociaty_boss_list, handle_sociaty_single_boss_get,
                              handle_sociaty_simple, handle_sociaty_boss_enter_count_buy,
                              handle_sociaty_boss_hp_push_payload, handle_sociaty_chess_result,
                              handle_sociaty_boss_rank_info, handle_sociaty_join_outtime_payload,
                              # ★★2026-10-08 妖盟弈局：6 条原本走 handle_sociaty_simple 的空桩
                              handle_sociaty_boss_chess_upgrade, handle_sociaty_boss_chess_num_buy,
                              handle_sociaty_boss_start, handle_sociaty_join_chess,
                              handle_sociaty_chess_hp, handle_sociaty_chess_hurt_info,
                              # ★★2026-10-11 妖盟红包 1187/1188/1189（会级共享实体 redbag）
                              handle_sociaty_redbag_list, handle_sociaty_redbag_send,
                              handle_sociaty_redbag_info,
                              # ★★2026-10-11 妖盟签到 1191（+ 8082 妖盟红点）
                              handle_sociaty_sign)
from handlers_sweep import (handle_sweep_enter, handle_sweep_start, handle_sweep_finish,
                            handle_redeem_code)
# ★★2026-10-11（A0 第 5 批）皮肤抽奖 4500/4501/4502/4504/4505/4506/4507
#   （表 D201/D202/D203/D204 本轮由 work\_table_migrate.py 从目标客户端迁入表根）
from handlers_skinlottery import (handle_skin_shop_info, handle_skin_shop_buy,
                                  handle_skin_lottery_start, handle_skin_box_push,
                                  handle_skin_home_info, handle_skin_get_box,
                                  handle_skin_get_have_skin)
# ★★2026-10-11（A0 第 7 批）烟花大会 1192~1197（`A09_1_ActivityFireworkRank` 已迁入表根）；
#   8086 是**推送**号（客户端 `NetFwsPlay` 只有 recvMessage），由 handlers_fws 主动推。
from handlers_fws import (handle_fws_activity, handle_fws_rank, handle_fws_rank_preview,
                          handle_fws_score_preview, handle_fws_get_reward, handle_fws_buy)
# ★★2026-10-11（A0 第 9 批）悬赏任务 1450~1454。数据源 `T43_TravelTasks`(35，奖励组 T02 全在)
#   + `T23_Function[ID=261/262/264/266]`。★1450 与 1454 **形状不同**（摊平 vs 嵌一层 + roleInfo）。
from handlers_posttask import (handle_post_task_info, handle_post_task_send,
                               handle_post_task_finish, handle_post_task_recieve,
                               handle_post_task_refresh)
# ★★2026-10-11（A0 第 10 批）任务界面 1311/1312/1313。数据源 `UG_07_Task`(405，`RewardID`
#   405 个组实测全在 `T02_Reward`) + `T20_StoryGroup`（17 个非 -1 的前置剧情 id 全部命中）。
#   ★实测三条：`TaskFinishPlotID` **405 行全是 -1**（没有后置剧情）；`TaskNextId` 不可信、
#   **`TaskPrevId` 才是严格单链**；`Quantity` 从 1 到 100000（是累计门槛，不是开关）。
from handlers_ugtask import (handle_ug_task_info, handle_ug_task_play, handle_ug_task_finish)
# ★★2026-10-11（A0 第 11 批）额外掉落活动 1190/1300。★**两张表的 DropId 在两个 id 空间**：
#   `UG07_ExtraReward.DropId=10010` -> **`G03_SpellGroup.GroupId`**（4 行、Desc 自带明文）；
#   `A08_ExtraReward.DropId=18001…` -> **`T02_Reward.GroupID`**（战斗掉落侧，另一批）。
#   ⚠ 1300 的 msg[2]（消耗三元组）与 msg[4]（完整 roleInfo）缺一个就走 `error_verify(nil) -> -1`
#     ⇒ 客户端对数字取索引当场报错。
from handlers_extradrop import handle_exdrop_info, handle_exdrop_exchange
# ★★2026-10-11（A0 第 12 批）特攻规则面板 12000（跨活动共用，"妖主·特攻·规则提示"）。
#   ★这些值是**运营/GM 配置**（`NetSpecialAtt.lua:33` 对 msg[1] 的注释就是"gm所需字段"），
#     两个客户端里**没有任何特攻表**（逐表核过）⇒ 本服没有可发的规则，硬凑伤害百分比就是灌假数值。
#   ★回**结构完整的 14 槽 + `switchIs=0`**（客户端自己写的"未开启"分支：不筛名单、不发事件 ⇒ 面板不出现）。
#     ⚠ 兜底 `[1,[[]]]` 更糟：`switchIs=nil` ⇒ `nil==0` 为**假** ⇒ 客户端**走进筛选分支**、
#     三个 attacktype 分支都不命中 ⇒ 名单落成**全部妖怪** ⇒ 弹出"所有妖怪都是特攻、文案全空"的**假规则**。
from handlers_specialatt import handle_specialatt
from handlers_sign import (handle_sociaty_sign_select, handle_sociaty_sign_use,
                           handle_sociaty_sign_buy, handle_guidance_task_data,
                           handle_guidance_task_award, handle_activity_limit_task,
                           handle_register, handle_role_create, handle_total_recharge,
                           handle_total_get_recharge, handle_sdk_buy, handle_shop_recharge,
                           handle_recharge_day_reward, handle_recharge_box_reward)
from handlers_story import (handle_story_enter, handle_story_drop, handle_story_finish,
                            handle_story_unlock_list,
                            # ★★2026-10-11 剧情任务/解锁 3502~3505（T20_2_StoryTaskList 已迁）
                            handle_story_task_info, handle_story_task_reward,
                            handle_story_unlock, handle_story_task_unlock_list,
                            handle_pvp_map_list, handle_pvp_get_defense_info,
                            handle_pvp_delete_defense_info, handle_pvp_change_name)
from handlers_activity import (handle_activity_list, handle_activity_receive,
                               handle_button_visible, handle_signin_info, handle_signin_get,
                               handle_push_switch, handle_operation_activity_list,
                               handle_nightmarket_info, handle_nightmarket_buy_join_num,
                               handle_nightmarket_join, handle_nightmarket_send_food,
                               handle_nightmarket_token_push, handle_nightmarket_shop_info,
                               handle_nightmarket_shop_buy,
                               # ★★2026-10-11 夜市排行榜/累计奖励 11611/11612
                               handle_nightmarket_rank_info, handle_nightmarket_box_reward,
                               handle_invade, handle_invade_result,
                               handle_aggress_tickets, handle_switch_role_info,
                               handle_switch_role_active, handle_switch_role_use,
                               handle_invade_rewards, handle_invade_get_rewards,
                               handle_limited_time_task, handle_limited_time_task_reward,
                               handle_aggress_reconnection, handle_lengend_consume,
                               handle_new_limited_time_task, handle_new_limited_time_award)
from handlers_qualifying import (handle_qualifying_home, handle_qualifying_reward,
                                 handle_qualifying_box_reward, handle_qualifying_report,
                                 handle_qualifying_rank, handle_qualifying_get_array,
                                 handle_qualifying_save_array, handle_qualifying_match,
                                 handle_qualifying_refresh_match, handle_qualifying_check_array,
                                 handle_qualifying_start, handle_qualifying_result,
                                 handle_qualifying_neting, handle_qualifying_arrange_neting)
from handlers_cardboss import (handle_cardboss_home, handle_cardboss_rank,
                               handle_cardboss_start, handle_cardboss_result)
from handlers_illustrate import handle_illustrate_get, handle_illustrate_single_get
from handlers_vitality import (handle_vitality_info, handle_vitality_buy,
                               handle_vitality_time, handle_vitality_push_begin,
                               handle_get_vitality, vitality_remain_push, vitality_timerid)
from handlers_tower import handle_tower_data, handle_tower_war_result
from handlers_train import (handle_train_get, handle_train_select, handle_train_result,
                            handle_train_reward)
from handlers_lottery import (handle_lottery_panel_info, handle_lottery_start,
                              handle_lottery_shop_info, handle_lottery_shop_buy,
                              handle_lottery_box_receive, handle_lottery_winners_list)
from handlers_profile import (handle_head_info, handle_head_buy, handle_head_frame_info,
                              handle_head_save, handle_head_frame_save,
                              handle_treasure_get, handle_treasure_buy, handle_treasure_achive,
                              handle_change_name, handle_friend_list,
                              handle_friend_add, handle_friend_ok_add, handle_friend_delete,
                              handle_friend_rcmds, handle_friend_update, handle_friend_send,
                              handle_friend_recv, handle_friend_onekey, handle_friend_search)
from handlers_chat import (handle_chat_send_msg, handle_chat_total_msg,
                           chat_push_frame_payload, chat_push_targets)
from handlers_specialty import (handle_specialty_upgrade, handle_specialty_sum_upgrade,
                                handle_specialty_change, handle_specialty_extension)
from handlers_cult import handle_monster_reset
from handlers_card import handle_gift_bag_use, topup_card_daily_mails
# ★2026-10-02 (任务 F / D3=C): 「客户端自报 userid」的身份解析。
#   单独一个模块的理由见 `acct_identity.py` 头部 —— 主要是**测试不必 import 本文件**
#   （import 本文件会在模块级把 sys.stdout 重定向进 work\logs\server-out.log）。
import acct_identity
from handlers_rank import (handle_ranking_defense, handle_ranking_qualifying,
                          handle_ranking_monster, handle_ranking_monster_info,
                          handle_invade_day_rank, handle_invade_his_rank,
                          handle_lengend_reward)
from handlers_opsact import (handle_activity_login, handle_activity_login_receive,
                             handle_activity_recharge, handle_activity_recharge_receive,
                             handle_one_rcg_info, handle_one_rcg_receive,
                             handle_consume_info, handle_consume_receive,
                             handle_recharge_activity, handle_activity_guide,
                             handle_fund_act_info, handle_fund_act_get_reward,
                             handle_fund_act_buy)

BASE = os.path.dirname(os.path.abspath(__file__))
# ★v32: 资源/CDN 根目录从本文件位置推导 (旧写法写死 r'<repo>\ygzz\extracted', 换盘/挪工程即全挂)。
#   优先级: 环境变量 YGZZ_ASSETS > 同级的 ../extracted。
ASSETS = os.environ.get('YGZZ_ASSETS') or os.path.normpath(os.path.join(BASE, '..', 'extracted'))
# ★2026-10-01 (B 任务) 地址可配置化 —— 原为字面量 `'0.0.0.0', 27001` / `'0.0.0.0', 18080`。
#
#   为什么要分成"bind"和"advertise"两组: 这两件事在多机场景下**必须分开**。
#     · bind     = 本进程往哪个网卡上听。默认 0.0.0.0 保持历史行为(adb reverse 与局域网都靠它)。
#     · advertise= 下发给客户端的地址。默认 127.0.0.1 **是硬要求**(A 方案/单机版的基础, 也是
#                  本机开发能跑通的原因) —— 绝不能把默认值改成局域网 IP。
#   为什么需要 advertise 单独可配: 公网/VPS/内网穿透下"客户端该连的外部地址"往往不等于
#     本机 bind 的地址, 也不等于本机端口(端口映射/NAT 会让外部端口 != 内部端口)。
#
#   环境变量(全部可选, 缺省 = 历史行为, 逐字节不变):
#     YGZZ_BIND_HOST        default '0.0.0.0'
#     YGZZ_ADVERTISE_HOST   default '127.0.0.1'   <- 必须与 config_data.py 的 ADV_HOST 同源同名
#     YGZZ_SOCK_PORT        default 27001         (27002 = SOCK_PORT+1, 同构, 见 start_game_socket)
#     YGZZ_CDN_PORT         default 18080
#     YGZZ_ADVERTISE_SOCK_PORT / YGZZ_ADVERTISE_CDN_PORT  default = 上面对应的本机端口
BIND_HOST = os.environ.get('YGZZ_BIND_HOST') or '0.0.0.0'
ADV_HOST = os.environ.get('YGZZ_ADVERTISE_HOST') or '127.0.0.1'
SOCK_HOST, SOCK_PORT = BIND_HOST, int(os.environ.get('YGZZ_SOCK_PORT') or 27001)
CDN_HOST, CDN_PORT   = BIND_HOST, int(os.environ.get('YGZZ_CDN_PORT') or 18080)
# 下发端口默认**跟随**监听端口 —— 单一事实来源。绝不在这里写第二份字面量。
ADV_SOCK_PORT = int(os.environ.get('YGZZ_ADVERTISE_SOCK_PORT') or SOCK_PORT)
ADV_CDN_PORT  = int(os.environ.get('YGZZ_ADVERTISE_CDN_PORT') or CDN_PORT)

# ============================================================================
# ★★2026-10-12 (多人单服): 运行模式开关
#
#   YGZZ_AUTH=1        开启「注册 / 口令 / 会话票据 / 游戏连接鉴权」。
#                      开启时 **必须** 同时开多账号（见下），否则所有人的档会落进同一个
#                      state.json —— 那不是"鉴权前"，那是"鉴权后照样串档"，比不做更危险。
#   YGZZ_MULTI_ACCOUNT=1  一人一档（accounts.py，2026-10-02 B 任务的落地）
#   YGZZ_DIAG=1|0      逐条响应诊断（默认开；`YGZZ_DIAG=0` 关掉）
#
# ⚠ 隔离回归（`_isolated_regression.py`）会**清空所有 `YGZZ_*` 变量**再跑套件，
#   所以这里的默认值决定回归网看到的行为。默认取"关"：
#   121 套回归网跑的是**单档假设**（见 accounts.py 开头："默认关闭 ⇒ 生产行为与改造前
#   逐字节一致，回归网那 77 套不受影响"）。要验鉴权就由套件自己显式打开。
#   本机的真实部署由 `work\ygzz_env.ps1` / 启动命令显式给这两个变量。
# ============================================================================
AUTH_ENABLED = os.environ.get('YGZZ_AUTH') == '1'
DIAG_ENABLED = os.environ.get('YGZZ_DIAG') != '0'

# ---- 限额（"限制帧长、请求体和连接数"的落地；全部可按需下调，上调要重跑压测）----
MAX_FRAME_BYTES   = int(os.environ.get('YGZZ_MAX_FRAME') or (1 << 20))   # 单帧 1 MiB
MAX_CONNS         = int(os.environ.get('YGZZ_MAX_CONNS') or 200)         # 同时在线连接数
MAX_REQS_PER_CONN = int(os.environ.get('YGZZ_MAX_REQS') or 200000)       # 单连接累计请求数
MAX_BYTES_PER_CONN = int(os.environ.get('YGZZ_MAX_CONN_BYTES') or (512 << 20))  # 单连接累计入站
MAX_HTTP_BODY     = int(os.environ.get('YGZZ_MAX_HTTP_BODY') or (64 << 10))     # HTTP 请求体 64 KiB

# ---- 诊断总线（进程级唯一）----
# ⚠ 必须在 `log()` 定义**之后**才能 `configure(log=log)`（`log` 是模块级函数，
#   定义在下面）。所以这里先建一个**不带 log 回调**的实例，`_bind_module_log()`
#   在 `log()` 定义之后再把它挂上 —— 顺序错了就是启动期 `NameError`（本轮真踩到）。
DIAG = ygzz_diag.configure(
    directory=os.path.join(BASE, 'logs', 'diag'), enabled=DIAG_ENABLED, log=None)

# ---- 鉴权（`AUTH` 为 None 表示未启用；启用后由 `__main__` 段 `auth_enable()` 建库）----
AUTH = None


def auth_enable():
    """建口令库 + 会话表。**只在真服务端入口调用**（测试要自己显式建）。"""
    global AUTH
    if not AUTH_ENABLED:
        return None
    if not accounts.ENABLED:
        # ★宁可拒绝启动，也不要"鉴权开着、档却是单档"：那是最坏的一种组合 ——
        #   玩家以为自己的账号独立，实际所有人的存档是同一个文件。
        raise RuntimeError(
            'YGZZ_AUTH=1 必须同时 YGZZ_MULTI_ACCOUNT=1 —— 否则鉴权通过了也只有一个共享存档。')
    root = os.path.normpath(os.path.join(BASE, 'accounts'))
    store = ygzz_auth.AuthStore(root, log=log)
    # ★ 票据寿命可被环境变量覆盖：**只**为了能实测"过期"这条判据
    #   （默认 900 秒，测试等不起）。生产不要设它。
    try:
        _ttl = float(os.environ.get('YGZZ_TICKET_TTL') or ygzz_auth.TICKET_TTL)
    except ValueError:
        _ttl = ygzz_auth.TICKET_TTL
    AUTH = {
        'store': store,
        'sessions': ygzz_auth.SessionRegistry(ttl=_ttl, log=log,
                                              on_revoke=_on_session_revoked),
        'lock': threading.RLock(),
    }
    _seed_auth_from_index(store)
    return AUTH


def _seed_auth_from_index(store):
    """把**磁盘上已有**的账号登记为"未认领"（保留既有玩家档的迁移映射）。

    改造前 `_accounts.json` 里已经有玩家（形如 `<peer> -> u0001`、
    `<userid> -> u0003`）。它们**没有口令**。不登记的话：
      · 要求口令 → 这三个玩家永远进不去（"角色没了"）；
      · 不要求口令 → 鉴权形同虚设。
    ⇒ 登记为 `legacy/未认领`，第一次用"账号名 + 口令"登录即认领（`AuthStore.claim`）。
      账号名优先取索引键里 `user:` 后面的那一段（那是玩家自己输的），否则用 peer（IP/主机名）。
    """
    try:
        idx = accounts.REGISTRY._load_index()
    except Exception as e:
        log('AUTH 存量账号登记：索引读取失败 %r（不阻断启动）' % (e,))
        return 0
    entries = []
    for key, aid in sorted((idx or {}).items()):
        if not isinstance(key, str) or not isinstance(aid, str):
            continue
        name = key[len(accounts._USER_KEY_PREFIX):] if key.startswith(
            accounts._USER_KEY_PREFIX) else key
        entries.append((name, aid))
    n = store.seed_legacy(entries, claimable=_claim_allow())
    if entries:
        log('AUTH 存量账号: %d 条，新登记 %d 条（未认领）' % (len(entries), n))
    return n


def _claim_allow():
    """允许**自助认领**的存量账号（逗号分隔）。

    ★★2026-10-26（审查 H2）：默认**空** —— "磁盘上有旧档"不再等于"谁先来谁设密码"。
      迁移时由运维显式给一次（`YGZZ_CLAIM_ALLOW=<account-a>,<account-b>`）或事后用
      `_auth_admin.py claim <账号>` 逐条放行；放行会写进 `_auth.json`（`claimable`），
      认领成功后自动关掉。别把"方便"做成常开 —— 那就是 H2 那条漏洞。
    """
    raw = os.environ.get('YGZZ_CLAIM_ALLOW') or ''
    return [x.strip() for x in raw.split(',') if x.strip()]


def _on_session_revoked(sess, why):
    """票据被顶掉/作废 → **主动断开**拿着它的那条连接。

    为什么必须断而不是"下次请求再拒"：客户端手里还拿着旧状态，它会继续发请求；
    让它在下一个写操作时才被拒，等于把"读改写交错"留给了一个不确定的窗口。
    断开 → 客户端自动重连 → 重走登录 → 拿到新票据（`ReconnectManager` 的既有行为）。

    ⚠⚠ 两条锁纪律（本轮实测踩到过一次真死锁）：
      ① **挑连接时持 `_LIVE_LOCK`，关连接时绝不持**；
      ② `_force_close` **不拿 `conn.lock`** —— 那条连接的线程可能正阻塞在
         `sendall`（对端不读）而持有该锁，去拿它就等于**把调这个函数的线程一起挂住**
         （实测形态：HTTP 线程卡死、客户端等不到应答、整条用例挂起直到超时）。
         `close()` 本身会让阻塞中的 `send`/`recv` 立刻返回错误，不需要先拿锁。
    """
    victims = []
    with _LIVE_LOCK:
        for lc in list(LIVE_CONNS.values()):
            if lc.session_ticket == sess.ticket or (
                    lc.userid is not None and lc.userid == sess.userid and lc.authed):
                lc.authed = False
                lc.account_id = None
                victims.append(lc)
    for lc in victims:
        _force_close(lc, 'session-%s' % why)
    if victims:
        log('AUTH 会话 %s(%s) 作废 -> 断开 %d 条连接' % (sess.userid, why, len(victims)))
    return len(victims)


def _force_close(lc, why):
    """从外部**逼停**一条连接：置标志 + 关 socket。

    **刻意不拿 `lc.lock`** —— 见 `_on_session_revoked` 的锁纪律②。
    `close()` 会让对端线程阻塞中的 `recv`/`sendall` 立刻返回错误，
    这正是我们要的"旧连接失效"；重复关一条连接则不是错误，吞掉。
    """
    try:
        lc.closed_reason = why
        lc.authed = False
        try:
            lc._s.close()
        except Exception:
            pass
    except Exception:
        pass


def http_base(port=None):
    """下发给客户端的 HTTP 根, 例: http://127.0.0.1:18080 。唯一构造点。"""
    return 'http://%s:%d' % (ADV_HOST, ADV_CDN_PORT if port is None else port)

def adv_base(hostport=False):
    """下发地址的人类可读摘要, 供启动横幅与自检使用。"""
    return '%s:%d' % (ADV_HOST, ADV_SOCK_PORT)

def log(s):
    s = time.strftime('%H:%M:%S') + ' | ' + s
    print(s, flush=True)

# 诊断总线的日志回调在这里才挂上（见上面 DIAG 处的说明：顺序不可换）。
DIAG._log = log


def acct_trace(s):
    """串档排查留痕（`work\\_acct_trace.json` 存在或 `YGZZ_ACCT_TRACE=1` 时开）。

    实现在 `accounts.trace()` —— **刻意不在这里自己写**：`state.save_state()` 也要留痕，
    而它一旦 `import game_server` 就会重跑本文件的模块级代码、把它的 stdout 换掉
    （实测把 `_saveguard_selftest` 打成 AttributeError）。所以留痕的唯一实现放在
    `accounts.py`，本文件只是转发。
    """
    return accounts.trace(s)

DEBUG_LOG = os.path.join(BASE, 'server_debug.log')
def dbg(s):
    try:
        with open(DEBUG_LOG, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S') + ' | ' + s + '\n')
    except Exception:
        pass

# MSG_ID（节选自 ConfigNet.lua）
MSG = dict(REGISTER=1001, ROLE_CREATE=1002, LOGIN=1003, ROLE_INFO=1004, GAME_SERVER=1005,
           SDK_LOGIN=1007, SESSION=1008, HEARTBEAT=1000, ROLE_NAME=1009, REDEEM_CODE=5000,
           CULTIVATE_INTENSITY=6006, CULTIVATE_LOAD=6004, CULTIVATE_UNLOAD=6005,
           CULTIVATE_DEVOUR=6002, CULTIVATE_EVOLVE=6003,
           CARD_TIME=8001, DRAW_CARD=8002,
           GIFT_BAG_GET=1097, PIECE_COMPOSE=1168, GIFT_BAG_USE=1098,
           MAIL_GET=1055, MAIL_DEL=1056, MAIL_RECEIVE=1057, MAIL_TOTAL=1059,
           FORMATION_LIST=3200, FORMATION_UPDATE=3201, FORMATION_SAVA=3202,
           MONSTER_LOCK=6001, CULTIVATE_ENCHANT=6007, EQUIP_LOCK=6008,
           SKIN_GET=1500, SKIN_REPLACE=1501,
           BAG_COMPOSE=1177, BAG_SELL=1178, BAG_USE=1179)

# 玩法系统 handler 分发: msgid -> (fn, 是否需要提取请求体json)
GAME_HANDLERS = {
    # 商店
    5001: (handle_shop_total, True), 5002: (handle_shop_buy, True), 5003: (handle_shop_update, False),
    # 战斗结算
    #   ⚠2026-09-12 (v28) 9004 曾在本表里注册两次(:147 handle_war_result / :153 handle_tower_war_result),
    #   Python dict 字面量**后者胜**且不报错 -> :147 那行是死代码(隐式覆盖, 交接档坑点 #55)。
    #   现按实际生效语义只保留一处: 9004 归妖塔进层 (handlers_tower.handle_tower_war_result)。
    #   ★v54c (2026-09-15): 这 5 条的 `value` 字段**语义各不相同**
    #     (9003/8006=官方奖励组id 密文, 9004=S51 TowerID 密文, 9005+=ObjectPosition),
    #     所以必须把协议号绑进共用处理器 —— 用 war_result_handler(mid) 而不是裸函数。
    #     9005/9006/9007 本版**尚未接** (ObjectPosition 语义未实证), mid 传进去只会让
    #     official_drop_group 返回 None -> 行为与旧版一致, 等 v54d 拿到真样本再开。
    9001: (war_result_handler(9001), True), 9003: (war_result_handler(9003), True),
    9005: (war_result_handler(9005), True),
    9006: (war_result_handler(9006), True), 9007: (war_result_handler(9007), True),
    # 任务/成就
    # ★v57: 1029 语义分流 —— 官方1.31 是 TASK_INFO（但客户端 NetTaskInfo:sendMessage 里有 return，永不发送），
    #   新包1.30 把它改名为 GETSKIN_LIST 且 **SuccessEnterGame.lua:45 每次进游戏无条件发**（铁律 #188）。
    #   两条请求 body 都是 {}、无法靠 body 区分，但老客户端实际不发 1029 ⇒ 直接按 GETSKIN_LIST 处理对两包都安全。
    1029: (handle_skin_get_list, False), 1041: (handle_task_get, True),
    # ★2026-10-02 (审计 §12.1): 1042 原先错路由到 handle_task_info(=1029 TASK_INFO 的处理器),
    #   回的是 id=1/2 的三字段行, 而成就界面的组 id 是 T33 的 1001–1005
    #   (AchievementList.lua:119-123) ⇒ 界面 5 行全空白。现改为专用 handler, 回 7 字段。
    #   1044 原与 1041 共用 handle_task_get(无视 achieveid、恒定发 100 铜钱), 现改为真领取。
    1042: (handle_achievement_info, False), 1044: (handle_achievement_get, True),
    # ★2026-10-02 (审计 §12.1): 101/102/104/105 是"进游戏必发"的推送请求, 客户端**不读应答体**;
    #   105 显式给一个应答形状(兜底 → 显式), 真正的亮灯由 push_reddots() 主动推 8082/1053 完成。
    105: (handle_point_request, False), 8082: (handle_attention_push, False),
    # 妖塔 (1034 数据, 9004 结算+进层; handlers_tower.py)
    1034: (handle_tower_data, False), 9004: (handle_tower_war_result, True),
    # 冲虚道场/挑战 (1025-1028 TRAIN; handlers_train.py)
    1025: (handle_train_get, False), 1026: (handle_train_select, True),
    1027: (handle_train_result, True), 1028: (handle_train_reward, True),
    # ★v30 新增: 系统解锁 1200 UMLOCK_SYS (PVEHome.lua:232/305/340 三入口: 妖塔/妖怪入侵/妖神传说)
    #   (v29 §2 认定的"服务端真缺口"; 未实装则等级门槛被客户端本地补丁绕过; handlers_sys.py)
    #   ⚠ 第二位必须 True: 该标志 = "是否把请求体传给 handler"(不是"是否写状态")。
    #     本 handler 需要 req{systype}; 若传 False, 会无参调用 -> body_json=None -> 误回 [100] 系统类型错误。
    1200: (handle_unlock_sys, True),
    # 剧情解锁关卡 1038 STORY_UNLOCKSCENE -> 大地图节点点亮清单 (按玩家等级, handlers_game.py)
    1038: (handle_story_unlock_scene, False),
    # ★v30 新增: 剧情章节列表 1037 STORY_UNLOCKLIST (StoryHome.lua:71 / StoryPlot.lua:512)
    #   (原为 B 类可达未覆盖: 不发则剧情界面章节列表空白; handlers_story.py)
    1037: (handle_story_unlock_list, False),
    # ★★2026-10-11（A0 第 4 批）剧情任务/解锁 3502~3505。修前四个号都不在表里。
    #   依赖两张表：`T20_StoryGroup` **补两列**（LockedItem/LockedTask）+ `T20_2_StoryTaskList`
    #   整张迁入（7 行，只在目标客户端），走 `work\_table_migrate.py`（写 + 回读双证）。
    #   · 3502 STORY_TASK_INFO  req{storyid} -> [[taskid,tasknum,taskstate]]（pairs 无守卫）
    #   · 3503 STORY_TASKREWARD req{taskid}  -> [roleInfo(**数组**), dropInfo]；537/538/539
    #   · 3504 STORY_UNLOCK     req{storyid} -> msg[1] 是**扁平三元组** [ucode,itemid,剩余count]
    #     （多发一层 -> 客户端 `updateItem` 读到 `_info[3]=nil` 直接 return，信物不减）
    #   · 3505 STORY_TASK_UNLOCKLIST req{} -> [[id, lockstate]]，**lockstate 0=未完成**
    #     （客户端源码注释写反了；`StoryHome.lua:81` 只在 ==0 时显示解锁蒙板）
    #   ⚠ 3505 只在 `getSysOpenV(11026)==1` 时被发（A04_JumpTag 的第 2 列 `TagId`）。
    3502: (handle_story_task_info, True), 3503: (handle_story_task_reward, True),
    3504: (handle_story_unlock, True), 3505: (handle_story_task_unlock_list, True),
    # PVE 列表 1035 (妖魔异闻计数; 客户端 NetPveList:recvMessage msg[1]=["id"..] 列表)
    1035: (handle_pve_list, False),
    # 邮件详情 1058 MAIL_READ (NetMailRead:recvMessage emailitem=msg[1] 27字段; 客户端点邮件读内容)
    1058: (handle_mail_read, True),
    # 妖神传说 1014 (LENGEND_GET: 按天轮换 + 刷新倒计时) / 妖怪入侵 1063 (AGGRESS_TODAY: 今日组)
    1014: (handle_lengend_get, False), 1063: (handle_aggress_today, False),
    # 每日任务 (1068/1069/1070, 客户端实际使用; 1029 在客户端已被禁用)
    1068: (handle_daily_task, False), 1069: (handle_daily_task_reward, True),
    1070: (handle_liveness_reward, True),
    # 探索
    1701: (handle_explore_enter, True), 1702: (handle_explore_vitality, True),
    1704: (handle_explore_drop, True), 1705: (handle_explore_degree, True),
    1706: (handle_explore_exit, True),
    # ★2026-10-11（新包 A0 第 1 批）补 1707 EXPLORE_REVICE / 1708 EXPLORE_REVICEINFO。
    #   客户端 `VWarFailure:requestMsg` 先看 106 PUSH_SWITCH 的 EXPLORE_REVICE 开关（服务端
    #   106 回空表=全开），随即发 1708；`timesState ~= 1` 就**把复活按钮整块藏起来**
    #   ⇒ 修前这两个号不在表里、`timesState` 恒 nil，探索内复活对 1.30 包等于不存在。
    #   口径（次数取 S01_Level.ReviveNum、消耗扣 T23[267]=1014 回魂丹、BI 打点不是扣道具）
    #   写在 handlers_game.py 的 `handle_explore_revive_info` 头部。
    1707: (handle_explore_revive, True), 1708: (handle_explore_revive_info, True),
    # ★2026-10-11（A0 第 2 批）在线奖励 1302~1304 + 限时新手任务剩余时间 4070。
    #   ⚠ 1303 是**当前线上唯一会打断 socket tick 的一条**：客户端
    #   `NetOnlineRewardRefresh.lua:36-38` 取完 `msg[2]` 直接 `info[1]`（无 `or {}`），
    #   而服务端此前没有 1303 ⇒ 兜底 `[1,[[]]]` ⇒ `msg[2]=nil` ⇒ 抛错，抛点
    #   `SocketManager.lua:556` **无 pcall** ⇒ 整条 tick 断掉。契约与口径见
    #   `handlers_onlinereward.py` 头部。
    1302: (handle_online_reward_info, True), 1303: (handle_online_reward_refresh, True),
    1304: (handle_online_reward_draw, True),
    #   4070：主界面「新手限时」按钮倒计时（`VMainButton:getLimitTaskTime` 每次进营地无条件发）；
    #   同系统 4068/4069 早在表里，只漏了这一条 ⇒ 按钮倒计时恒 0。
    #   剩余时间与 4068 的 msg[4] 共用 `handlers_activity._nlt_remain`（不新开记账键）。
    4070: (handle_limited_time_new_task_time, False),
    # ★2026-10-11 补 3500 NET_SHOW_REWARD（装机客户端的"本次探索累计获得"结算面板）。
    #   它此前**不在表里** ⇒ 落 `resp_for(3500)` 兜底 `[1,[[]]]` ⇒ 客户端恒打
    #   「很遗憾，本次探索没有获得任何物品」（真机 E1 验收现象，归档里的未定因项）。
    #   契约与口径写在 `handlers_game.py` 的 `handle_explore_show_reward` 头部。
    3500: (handle_explore_show_reward, True),
    # ★2026-10-11（新包 A0 第 1 批）补 3501 NET_GET_BOX_REWARD（解锁度门控宝箱领奖）。
    #   服务端此前没有 3501 ⇒ 客户端 `NetGetBoxReward:recvMessage` 拿到兜底体，
    #   箱子点了没反应；1705 的 `msg[5]` 同批补上（宝箱状态的真值来源）。
    3501: (handle_explore_box_reward, True),
    # 头像/宝箱 (handlers_profile.py)
    1092: (handle_head_info, False), 1093: (handle_head_buy, True),
    1094: (handle_head_frame_info, False), 1095: (handle_head_save, True),
    1096: (handle_head_frame_save, True),
    1060: (handle_treasure_get, False), 1061: (handle_treasure_buy, True),
    1062: (handle_treasure_achive, True),
    # 技能 (1502-1505, handlers_skill.py)
    1502: (handle_skill_chip_compose, True), 1503: (handle_skill_study, True),
    1504: (handle_skill_intensify, True), 1505: (handle_skill_replace, True),
    # 进化
    6003: (handle_monster_evolve, True),
    # PVP (handlers_pvp.py, 机器人对战)
    7001: (handle_pvp_get_bat_arr, False), 7002: (handle_pvp_save_bat_arr, True),
    7003: (handle_pvp_unlock_map, True), 7006: (handle_pvp_receive, True),
    7007: (handle_pvp_one_key_receive, False), 7008: (handle_pvp_enter, False),
    7009: (handle_pvp_war_result, True), 7010: (handle_pvp_match, False),
    7011: (handle_pvp_get_box_rewards, False), 7012: (handle_pvp_receive_box, True),
    # 体力 (handlers_vitality.py; 体力=道具1006)
    1074: (handle_vitality_info, False), 1075: (handle_vitality_buy, False),
    1076: (handle_vitality_push_begin, False), 1100: (handle_vitality_time, True),
    # 卡池BOSS (handlers_cardboss.py)
    8003: (handle_cardboss_rank, True), 8004: (handle_cardboss_home, True),
    8005: (handle_cardboss_start, True), 8006: (handle_cardboss_result, True),
    # 活动 (7013/7014/7016/7100/7101; 8日登录 + 30日签到; handlers_activity.py)
    7013: (handle_activity_list, False), 7014: (handle_activity_receive, True),
    7016: (handle_button_visible, False), 7100: (handle_signin_info, False),
    7101: (handle_signin_get, True),
    1101: (handle_activity_cards, False), 1108: (handle_operation_activity_list, False),
    # 探索形象 1184/1185/1186 (S20_ExploreBody; roleInfo[29]; handlers_activity.py)
    1184: (handle_switch_role_info, False), 1185: (handle_switch_role_active, True),
    1186: (handle_switch_role_use, True),
    # 图鉴 1072 ILLUSTRATE_GET / 1073 ILLUSTRATE_SINGLEGET (handlers_illustrate.py; 2026-09-12 实装)
    1072: (handle_illustrate_get, False), 1073: (handle_illustrate_single_get, True),
    # 推送开关 106 (被关闭系统tag列表; 空=全开) / 妖主大入侵 1102+9008 / 夜市 1161-1167
    106: (handle_push_switch, False),
    1102: (handle_invade, False), 9008: (handle_invade_result, True),
    1064: (handle_aggress_tickets, True),
    # ★2026-09-12 补形状 (用户反馈 §1 界面卡死): 1104/1105 妖主入侵奖励, 3068/3069 限时任务
    #   旧: 无处理器 -> resp_for 默认 [1,{}] -> msg[1]=nil -> NetInvadeRewards `pairs(msg[7])` /
    #       NetGetLimitedTimeTask `pairs(msg[1])` 抛错 -> SocketManager recvMessage 中断 -> 卡死
    1104: (handle_invade_rewards, False), 1105: (handle_invade_get_rewards, True),
    3068: (handle_limited_time_task, False), 3069: (handle_limited_time_task_reward, True),
    1161: (handle_nightmarket_info, False), 1162: (handle_nightmarket_send_food, True),
    1163: (handle_nightmarket_buy_join_num, False), 1164: (handle_nightmarket_join, False),
    1165: (handle_nightmarket_shop_info, False), 1166: (handle_nightmarket_shop_buy, True),
    1167: (handle_nightmarket_token_push, False),
    # ★★2026-10-11（A0 第 6 批）夜市排行榜/累计奖励 11611/11612。
    #   数据源 `A06_3_ActivityMarketReward`(14) **只在目标客户端**，本批迁入表根。
    #   · 11611 msg[1] 必须 ≥2 槽（客户端直接取 `msg[1][2]` 当"我这一行"）、msg[2] 必须存在
    #     （扁平 6 项，1=已领/2=未领/3=未完成；客户端注释写 0/1/2 是**错的**）。
    #   · **未上榜回 `rank = -1`**：`VNMarketReward.lua:156` 判 `top == -1` 才显示"未上榜"。
    #   · 11612 的 `boxIndex` 是 **0-based**（`Button_10:setTag(_idx - 1)`），而同一份数据在
    #     11611 的 msg[2] 里是 1-based —— 客户端自身不一致，服务端只能照它写。
    11611: (handle_nightmarket_rank_info, False), 11612: (handle_nightmarket_box_reward, True),
    # ★★2026-10-11（A0 第 7 批）烟花大会 1192~1197。1108 开闸后这 6 个号客户端**真的会发**。
    #   ⚠ 1193 的 msg[1]/msg[2] 必须是**数字且单位毫秒**（客户端 `tonumber(msg[1])/1000`）；
    #     1196/1197 的 msg[1] 必须是**完整 roleInfo 数组**（`gamer_setRoleInfo` 逐字段读）；
    #     1195 的 msg[1] **只包一层**（客户端源码里"服务器回的数据不对 多一层"那句注释就是疤）；
    #     1193 的"未上榜"回 **-1**（`FwsRanks.lua:220` 判 `getTop() <= 0`）。
    #   · 8086 ACT_FWS_PLAY 是**推送**号（客户端只有 recvMessage），不在这里登记；
    #     它在"使用烟花礼包 2052/2053"成功后被 handlers_bag → handlers_fws 推出去。
    1192: (handle_fws_activity, True), 1193: (handle_fws_rank, True),
    1194: (handle_fws_rank_preview, True), 1195: (handle_fws_score_preview, True),
    1196: (handle_fws_get_reward, True), 1197: (handle_fws_buy, True),
    # ★★2026-10-11（A0 第 9 批）悬赏任务 1450~1454（旧名"巡游任务"，客户端文案是"悬赏"）。
    #   数据源 `T43_TravelTasks`(35，`Reward` 组实测 35 个全在 `T02_Reward`)；槽位数与等级档取
    #   `T23_Function[ID=261]=13|25|30` 与 `[ID=262]=3|4|5`（**客户端那段有 max(1,idx-1)**，
    #   实际是 <25 开 3 / 25~29 开 4 / ≥30 开 5，服务端必须算同一个数）。
    #   ⚠ `flushCost`（刷新消耗量）**无表源**，现取 0（免费刷新）并记进待拍板；
    #     1452"立即完成"的价位在表里（`FinishPrice`），货币取 `T23[ID=264]`=3 铜钱。
    #   ★ 1450 = `[1,[taskList, refreshTime, flushCost, hadSendCount]]`（四槽摊平）；
    #     1454 = `[1,[roleInfo, [同样四槽]]]`（**多一层** + 首槽 roleInfo）—— 两者不能互抄。
    1450: (handle_post_task_info, False), 1451: (handle_post_task_send, True),
    1452: (handle_post_task_finish, True), 1453: (handle_post_task_recieve, True),
    1454: (handle_post_task_refresh, False),
    # ★★2026-10-11（A0 第 10 批）任务界面 1311/1312/1313。
    #   · 1311 = `[1,[主线, 支线, 悬赏, 日常]]`，每块 `[总数, 完成数, infoList]`、行 **8 槽**；
    #     `UG_07_Task` 只有 TaskType 1/2 ⇒ 后两块回**空段**（悬赏是 T43 + 1450~1454 那套）。
    #   · 1312 = **纯 ack**（客户端只回调），真正的作用是落 `aStory`/`fStory`；
    #   · 1313 = `[roleInfo, dropInfo]`；状态机 0 不可接/1 未接/2 进行中/3 未领奖/4 已领奖。
    1311: (handle_ug_task_info, False), 1312: (handle_ug_task_play, True),
    1313: (handle_ug_task_finish, True),
    # ★★2026-10-11（A0 第 11 批）额外掉落活动 1190/1300（"打怪掉兑换币 → 换礼包"，Activityype=18 走公告页）。
    #   1190 = `[1,[jumpId, 描述, 起ms, 止ms, 底图, 我的兑换币, 栏位列表]]`；
    #   1300 = `[ownNum, [ucode,道具id,消耗数], dropInfo, roleInfo]`（**四槽齐全**，见上方 import 处的警告）。
    1190: (handle_exdrop_info, False), 1300: (handle_exdrop_exchange, True),
    # ★★2026-10-11（A0 第 12 批）特攻规则面板 12000。要请求体（`{activityType}`）。
    #   ★必须回**结构完整的 14 槽**（不能回 `[1,[[]]]`）：见上方 import 处的注释 —— 少槽位会让
    #     客户端把 nil 当 `switchIs`、判 `~= 0` 为真，从而走进筛选分支弹出**假规则面板**。
    12000: (handle_specialatt, True),
    # 竞技/排位 (1078-1091, handlers_qualifying.py; 机器人对战 + 半月赛季键)
    1078: (handle_qualifying_home, False), 1079: (handle_qualifying_reward, False),
    1080: (handle_qualifying_box_reward, True), 1081: (handle_qualifying_report, False),
    1082: (handle_qualifying_rank, True), 1083: (handle_qualifying_get_array, False),
    1084: (handle_qualifying_save_array, True), 1085: (handle_qualifying_match, False),
    1086: (handle_qualifying_refresh_match, False), 1087: (handle_qualifying_check_array, False),
    1088: (handle_qualifying_result, True), 1089: (handle_qualifying_neting, False),
    1090: (handle_qualifying_arrange_neting, True), 1091: (handle_qualifying_start, True),
    # 公会 (handlers_sociaty.py)
    1121: (handle_sociaty_info, False), 1122: (handle_sociaty_list, False),
    1123: (handle_sociaty_create, True), 1133: (handle_sociaty_donate, True),
    # ★★2026-10-11（A0 第 3 批）1191 SOCIATY_SIGN 妖盟签到。
    #   客户端 `NetSociatySign:recvMessage` **一个槽都不读** ⇒ 应答体 `[1,[]]` 足够；
    #   但**不实装它按钮就是死的**：`VSociatyInformation:onTick`（每秒）读
    #   `getState(UNOIN=11, MESSAGE=3, SIGN=1)`，假就 `setEnabled(false)` + 文案"已签到"，
    #   而全文件**没有第二处 `setEnabled(true)`** ⇒ 不进面板 1 秒后按钮就按不动。
    #   ⇒ 实装 = ①记 `EQ_STATE['sociaty']['sign_date']` ②推 `8082 [[11,3,1,bool]]`
    #     （推给**每个成员各自的**载荷，见 handlers_point.push_attention）。
    #   ⚠ 周任务进度落 `sociaty.wk_ct`（UN15 1035 要"一周 5 次"，**不能**塞每日的 a07_ct）。
    1191: (handle_sociaty_sign, False),
    # ★★2026-10-11（新包 A0 第 3 批）妖盟红包 1187/1188/1189。
    #   修前这三个号**不在表里** ⇒ 兜底 `[1,[[]]]`，而它恰好是"成功 + 空列表"：
    #     1188 的客户端拿到 statecode=1 **就本地扣鬼道积分**（`NetSociatySendRedBag.lua:43`），
    #     于是玩家看到「积分扣了，红包不见了」；1189 显示「恭喜获得 0」；1187 恒空。
    #   真值落点 = 会级共享实体 `world['guilds'][gid]['redbag']`（照 chess 写法），
    #   语义/错误码出处（T41_CodeKey 1219 规则 + codeValue 30404~30411）见 `guild_world.py` 段头。
    #   ⚠ 三个都**必须**传 body（第三位 True）：1188/1189 的请求体带 moneyNum/redId。
    1187: (handle_sociaty_redbag_list, True), 1188: (handle_sociaty_redbag_send, True),
    1189: (handle_sociaty_redbag_info, True),
    # 特性系统 (handlers_specialty.py; 客户端 ConfigNet: SPECIALTY_SUMUPGRADE=1180 / UPGRADE=1181
    #   / CHANGE=1182 / EXTENSION=1183 —— 注意 6007/6008 不是特性消息号!)
    1180: (handle_specialty_sum_upgrade, True), 1181: (handle_specialty_upgrade, True),
    1182: (handle_specialty_change, True), 1183: (handle_specialty_extension, True),
    # 幸运抽奖/兑换商城 (1401-1406; handlers_lottery.py, D101-D104 表驱动)
    1401: (handle_lottery_panel_info, True), 1402: (handle_lottery_start, True),
    1403: (handle_lottery_shop_info, True), 1404: (handle_lottery_shop_buy, True),
    1405: (handle_lottery_box_receive, True), 1406: (handle_lottery_winners_list, True),
    # ★★2026-10-11（A0 第 5 批）皮肤抽奖 4500~4507（**4503 不存在**，客户端也没定义）。
    #   ⚠ 与上面那组**完全无关**：1401 那套是 `D101~D104`（妖魂占卜），这一套是
    #     `D201~D204`（皮肤抽奖克隆版）—— 两张表内容不同（哈希确认过），别互相替代。
    #   ⚠ 4502 必须回**四槽**：目标客户端 `A04_JumpTag` 的 `TagId=11025` 行为 `FunctionCHS='1'`
    #     ⇒ 客户端走 `getSysOpenV(11025)` 为真的分支，`msg[4]` 才是 `[roleInfo, dropInfo]`；
    #     回三槽会让它拿数字当 roleInfo 传给 `gamer_setRoleInfo` 崩。
    #   ⚠ 4504 是**推送**号（客户端只被动收），登记它是为了让"客户端真发了"时也有正确应答。
    4500: (handle_skin_shop_info, True), 4501: (handle_skin_shop_buy, True),
    4502: (handle_skin_lottery_start, True), 4504: (handle_skin_box_push, True),
    4505: (handle_skin_home_info, True), 4506: (handle_skin_get_box, True),
    4507: (handle_skin_get_have_skin, True),
    # 吞噬重置 (handlers_cult.py; 6009 无启树根)
    6009: (handle_monster_reset, True),
    # ★2026-09-12 (v26 P1) 附魔 6007 / 锁定 6001+6008 (handlers_equip.py)
    #   6007 旧实现缺失 -> 兜底 [1,[[]]] -> 客户端 SQGhost.lua:52 `table index is nil` -> socket tick 断
    #   6001/6008 旧实现缺失 -> 兜底 msg[1]={} -> equip_updateItem({}) 把装备行写坏 + 锁定不生效
    6007: (handle_equip_enchant, True),
    6001: (handle_monster_lock, True), 6008: (handle_equip_lock, True),
    # ★2026-09-12 (v26 P1) 排行榜全家桶 + 妖神结算 (handlers_rank.py)
    #   1049/1050/1065/1066: 未处理时 msg[1]=nil -> `string.find(nil,"+")` 直接抛错 -> 排行榜点开即卡死
    #   1106/1107: 未处理时 msg[3]=nil -> VInvadeRanking 取值崩 (v25 真机实测)
    #   9002: 未处理时 msg[2]=nil -> pairs(nil) (妖神传说结算永远走不到)
    1049: (handle_ranking_defense, True), 1050: (handle_ranking_qualifying, False),
    1065: (handle_ranking_monster, True), 1066: (handle_ranking_monster_info, True),
    1106: (handle_invade_day_rank, True), 1107: (handle_invade_his_rank, True),
    9002: (handle_lengend_reward, True),
    # ★2026-09-12 (v26 P1) 改名 2001 / 好友列表 2100 (handlers_profile.py)
    #   2001: 兜底 msg[1]={} 被当 roleInfo 灌进客户端 -> 金币/等级/体力全 nil
    #   2100: 兜底 msg[2]=nil -> `pairs(nil)` 抛错 (MFriendHome/ChatPrivate/PVEView 三处调用点)
    2001: (handle_change_name, True), 2100: (handle_friend_list, False),
    # ★2026-09-12 (v26 P1) 运营活动族 (handlers_opsact.py) —— v25 A 类的 1109/1113/1115/1150/1169/1811
    #   全部未处理 -> 兜底 [1,[[]]] -> 客户端 pairs(msg[5])/pairs(msg[8]) 拿到 nil -> 抛错 -> socket tick 断
    1113: (handle_activity_login, True), 1114: (handle_activity_login_receive, True),
    1115: (handle_activity_recharge, True), 1116: (handle_activity_recharge_receive, True),
    1150: (handle_one_rcg_info, True), 1151: (handle_one_rcg_receive, True),
    1169: (handle_consume_info, True), 1170: (handle_consume_receive, True),
    1109: (handle_recharge_activity, True), 1112: (handle_activity_guide, False),
    1811: (handle_fund_act_info, True), 1812: (handle_fund_act_get_reward, True),
    1813: (handle_fund_act_buy, True),
    # ★2026-09-12 (v26 P1) A 类剩余三件 (handlers_activity.py)
    #   1012: 兜底 msg[4]=nil -> pairs(nil) -> 妖主入侵断线重连直接卡死
    #   1017: 兜底 msg[2]=nil -> SQGhost.lua:273 nil 算术 -> 妖神传说走不到战斗 (入口断)
    #   4068/4069: 兜底 msg[2]=nil -> pairs(nil) -> 新版限时任务面板点开即卡
    1012: (handle_aggress_reconnection, False), 1017: (handle_lengend_consume, True),
    4068: (handle_new_limited_time_task, False), 4069: (handle_new_limited_time_award, True),
    # ★2026-09-12 (v26 P2) 好友族 2101~2109 (handlers_profile.py)
    #   2104 `pairs(msg[1])` / 2108 msg[1..5] 直取 / 2106·2107·2109 msg[1] 当 roleInfo -> 未处理时崩或数值错乱
    2101: (handle_friend_add, True), 2102: (handle_friend_ok_add, True),
    2103: (handle_friend_delete, True), 2104: (handle_friend_rcmds, False),
    2105: (handle_friend_update, False), 2106: (handle_friend_send, True),
    2107: (handle_friend_recv, True), 2108: (handle_friend_search, False),
    2109: (handle_friend_onekey, False),
    # ★2026-09-12 (v26 P2) 聊天 8080/8081 (handlers_chat.py)
    #   8081 是**服务端主动推**的全量记录 (客户端 sendMessage 请求体为空) -> 不推 = 世界/私聊/妖盟三个页签永远空白
    #   8080 未处理时只回调不崩, 但消息丢进黑洞
    8080: (handle_chat_send_msg, True), 8081: (handle_chat_total_msg, False),
    # ★2026-09-12 (v26 goal-round-2) 公会族补齐 1124~1160 (handlers_sociaty.py)
    #   真崩 4 条: 1125 `pairs(msg[1])` / 1135 `pairs(msg[1])+pairs(msg[3])` /
    #              1143 `meMsg=msg[2]` 后直取(无守卫) / 1155 `msg[1][1]`+`ipairs(msg[2])`+`msg[3][4]`
    #   其余"只回调"型: 兜底不崩但点了没反应
    1125: (handle_sociaty_join_list, False), 1126: (handle_sociaty_apply_join, False),
    # ★2026-09-12 (v28) 1124 SOCIATY_APPLY_JOIN 补注册 —— 此前 1121~1160 唯独缺它 (坑点 #54)。
    #   注意不要复用 handle_sociaty_apply_join: 那个是 1126(入会审批) 的处理器, 语义不同。
    1124: (handle_sociaty_apply_join_req, True),
    1127: (handle_sociaty_kick_out, True), 1128: (handle_sociaty_notice, True),
    1129: (handle_sociaty_quit, False), 1130: (handle_sociaty_post_change, True),
    1131: (handle_sociaty_join, True), 1132: (handle_sociaty_join_type, True),
    1134: (handle_sociaty_data_payload, False), 1135: (handle_sociaty_week_task, False),
    1136: (handle_sociaty_task_reward, True), 1137: (handle_sociaty_liveness_reward, True),
    1138: (handle_sociaty_shop_info, False), 1139: (handle_sociaty_shop_buy, True),
    1140: (handle_sociaty_dismiss, False), 1141: (handle_sociaty_sync_info, False),
    1142: (handle_sociaty_shop_upgrade, False), 1143: (handle_sociaty_rank_info, False),
    1144: (handle_sociaty_boss_list, False), 1145: (handle_sociaty_single_boss_get, True),
    # ★★2026-10-08 妖盟弈局（1144~1159）：修前 1146/1147/1148/1156/1157/1158 六条
    #   **客户端真的会发**的请求统一走 `handle_sociaty_simple` -> `[1,[]]`。
    #   客户端 `recvMessage` 只判 statecode ⇒ 按钮一律"成功"，服务端却什么都没记住
    #   （等级/次数/血量/伤害榜全是 per-account 假值，队友互不可见）。
    #   现在接到世界键 `guilds[gid]['chess']`（guild_world），单档仍走原兼容分支。
    #   `_nb`（是否读请求体）：1146/1147 空包；1148/1156/1157/1158 带 `bossid`(+hp)。
    1146: (handle_sociaty_boss_chess_upgrade, False),
    1147: (handle_sociaty_boss_chess_num_buy, False),
    1148: (handle_sociaty_boss_start, True),
    1156: (handle_sociaty_join_chess, True),
    1157: (handle_sociaty_chess_hp, True),
    1158: (handle_sociaty_chess_hurt_info, True),
    1149: (handle_sociaty_boss_enter_count_buy, False),
    1152: (handle_sociaty_boss_hp_push_payload, False), 1153: (handle_sociaty_chess_result, True),
    #   1154（弈局结束）/1159（开启通知）是**纯下行推送**号：客户端没有 sendMessage，
    #   只能由服务端主动推（见 `handlers_sociaty._push_chess_over/_push_chess_start_notice`）。
    #   这里的注册只保证"万一客户端发来"不崩；`_out`/`[1,[]]` 都行。
    1154: (handle_sociaty_simple, False), 1155: (handle_sociaty_boss_rank_info, True),
    1159: (handle_sociaty_simple, False),
    1160: (handle_sociaty_join_outtime_payload, False),
    # ★2026-09-12 (v26 goal-round-2 续) 扫荡 1118/1119/1120 + 兑换码 5000 (handlers_sweep.py)
    #   ★1119/1120 是**修正覆盖率口径后新暴露的两条 A 类崩点**: 客户端直取 `prop[1]`
    #     (`SQManager:prop_getMe():updateItem(prop[1], prop)`) -> 兜底 msg[2]=nil -> prop[1] 崩
    1118: (handle_sweep_enter, True), 1119: (handle_sweep_start, True),
    1120: (handle_sweep_finish, True), 5000: (handle_redeem_code, True),
    # ★2026-09-12 (v26 goal-round-3) 公会徽记 1814/1815/1816 + 新手引导任务 2068/2069
    #   + 旧版限时任务 3070 (handlers_sign.py)
    #   静态表: 徽记 -> UN04_UnionIcon(9 行) / 新手任务 -> UG_05_GuideTask(23 行)
    #   ★3070 的 msg[1..8] 全部无守卫, 且 [4]/[5] 客户端会 /1000 -> 必须给数字毫秒
    1814: (handle_sociaty_sign_select, False), 1815: (handle_sociaty_sign_use, True),
    1816: (handle_sociaty_sign_buy, True),
    2068: (handle_guidance_task_data, False), 2069: (handle_guidance_task_award, True),
    3070: (handle_activity_limit_task, False),
    # ★2026-09-12 (v26 goal-round-3 收尾) 注册/建角 + 充值族 (handlers_sign.py)
    #   1001/1002: 新账号必经路径 (兜底会让 gamer_setAccount(nil) 写坏账号)
    #   5007 充能商店列表 / 5004 累计充值列表 / 5005·1110·1111 充值奖励 / 5006 SDK 下单
    #   ★ 私服对充值族统一策略: **可看不可买** (列表按真表下发, 购买/领取明确失败, 不假装成功)
    1001: (handle_register, True), 1002: (handle_role_create, True),
    5004: (handle_total_recharge, False), 5005: (handle_total_get_recharge, True),
    5006: (handle_sdk_buy, True), 5007: (handle_shop_recharge, True),
    1110: (handle_recharge_day_reward, True), 1111: (handle_recharge_box_reward, True),
    # ★2026-09-12 (v26 goal-round-4) 剧情 4443/4444/4445 + 妖怪对战防守族 1036/7004/7005/7015
    #   (handlers_story.py)
    #   剧情: 客户端靠本地表推进, 服务端做"等级校验 + 掉落发放 + 已看过记录"
    #   1036: 客户端 sendMessage 是 `if true then return end` -> 只能服务端主动推
    4443: (handle_story_enter, True), 4444: (handle_story_drop, True),
    4445: (handle_story_finish, True),
    1036: (handle_pvp_map_list, False), 7004: (handle_pvp_get_defense_info, False),
    7005: (handle_pvp_delete_defense_info, True), 7015: (handle_pvp_change_name, True),
    # ★2026-09-12 (v30 施工轮 3) 评论三件套 1171/1172/1173 (handlers_comment.py)
    #   1171 NetCommentInfo  req{id,type,page} -> [总数, [[7字段]...]]
    #        ⚠ VCommentHome.lua:295 `os.date("%Y.%m.%d", commentTime / 1000)` -> commentTime 必须毫秒非 nil
    #        ⚠ 分页: 客户端 totalPage=ceil(commentNum/10) -> 第1页要回**总数**, 不是本页条数
    #   1172 NetCommentPraise req{commentId,staticId,type} -> recvMessage 只 emit, 不读字段 -> [1,[]]
    #   1173 NetSendComment   req{id,type,commentContent} -> 与 1171 同形状, emit 第3参=2
    #   type 两套编号: 查列表 1/2/3, 发+赞 7/8/9(= +6) -> handler 内统一归一
    1171: (handle_comment_info, True), 1172: (handle_comment_praise, True),
    1173: (handle_comment_send, True),
    #   ★同轮补齐: 三条"只查 statecode、不读任何字段"的协议 —— 兜底本就不崩,
    #     但显式登记可让"未覆盖"归零, 并把客户端上行的数据落一份到存档(便于排查)。
    #   1067 NetRankingSendAttr req{data}   : 妖神排位排行榜上报怪物属性 (纯上传, 无回包字段)
    #   1174 NetRankingPraise   req{staticId,type} : 排行榜点赞 -> emit 后 UI 自刷
    #   1607 NetWarVerification req{}       : 战斗结果校验回执 (客户端发**空包**)
    1067: (handle_rank_send_attr, True), 1174: (handle_ranking_praise, True),
    1607: (handle_war_verification, True),
    # ★2026-09-12 (v30 施工轮 4) B 类残留 13 条收口 (handlers_batch.py)
    #   【甲类·只判 statecode / 只回调 -> [1,[]]】
    #     100 PUSH_SHOP / 1071 SHARE / 1400 SELECT_LANGUAGE : 纯确认
    #     1010 AGGRESS_WAVE / 1011 AGGRESS_FORMATION        : 妖怪入侵波次+布阵上报(错误码 524/525/526/527)
    #     1054 POINT_UPDATE                                 : 小红点推送确认
    #     1234 MONSTER_DAMAGE                               : 战斗伤害上报
    #     1052 LIMITGIFT_RECEIVE                            : roleInfo+dropInfo; 无计费 -> 一律拒绝(580/581/582)
    #   【乙类·需要真数据】
    #     1043 ACHIEVEMENT_DETAIL : req{id=SystemID, level=LevelID} -> [id,groupid,value,state]
    #     1051 LIMITGIFT_LIST     : 私服无计费 -> 空列表(入口自然隐藏), 不给"永远领不到"的诱导界面
    #     1288 AD_NOTICE          : A01_Activity 真数据; ★子表必须非空否则客户端字段全 nil
    #     1810 FUND_ACT_LIST      : A31_1_FundActivities 真 ID(客户端按 id 查表建 SD 对象)
    #     2000 ACTIVITY_NOTICE    : A01_Activity 真数据, 时间转毫秒
    100: (handle_push_shop, False), 1010: (handle_aggress_wave, True),
    1011: (handle_aggress_formation, True), 1054: (handle_point_update, True),
    1071: (handle_share, False), 1234: (handle_monster_damage, True),
    1400: (handle_select_language, True), 1052: (handle_limitgift_receive, True),
    1043: (handle_achievement_detail, True), 1051: (handle_limitgift_list, False),
    1288: (handle_ad_notice, False), 1810: (handle_fund_act_list, False),
    2000: (handle_activity_notice, False),
}

# ---------- 游戏 socket: 帧收发 (假定 [4字节长度][4字节msgid][4字节seqid][json]) ----------
def recv_exact(conn, n):
    buf=b''
    while len(buf)<n:
        d=conn.recv(n-len(buf))
        if not d: break
        buf+=d
    return buf

def read_frame(conn):
    hdr=recv_exact(conn,12)
    if len(hdr)<12: return None
    total,msgid,seqid = struct.unpack('<III', hdr)
    # ★2026-10-12 (限额): `total` 是**客户端自己声明的**长度 —— 不设上限就等于让对端
    #   用"我要发 4 GB"让我们一直 `recv` 下去。上限同样取 MAX_FRAME_BYTES。
    if total < 12 or total > MAX_FRAME_BYTES:
        raise ValueError('LE 帧长越界 total=%r (上限 %d)' % (total, MAX_FRAME_BYTES))
    payload=recv_exact(conn, total-12) if total>12 else b''
    if len(payload) != max(0, total-12):
        return None
    conn.bytes_in = int(getattr(conn, 'bytes_in', 0)) + 12 + len(payload)
    try: obj=json.loads(base64.b64decode(payload).decode('utf-8','replace')) if payload else {}
    except Exception: obj={'raw':payload.decode('utf-8','replace')}
    return msgid,seqid,obj

# --- CGMessage receive-frame structure (confirmed via Ghidra decompile of CNetManager::ProcessInputStream) ---
# frame = [31-byte header: u32BE framelen | 5×u32BE reserved | u16BE count]
#         [ count × ( u32BE type | u32BE seqid | u32BE bodysize | body ) ]
#   body for a CGMessage = [ u16BE msglen | msgjson ]
def pack_login_frame(msgid, seqid, json):
    body = struct.pack('>H', len(json)) + json                    # CGMessage body [u16BE len][json]
    msg  = struct.pack('>III', 10000, msgid, len(body)) + body    # class=10000(generic CGMessage) | logical msgid | bodysize | body
    count = 1
    # 31-byte header (client reads Peek/Read(...,0x1f)):
    #  offset0 u32BE framelen | offset4 u32BE | offset8 u32BE+1B | offset13 u32BE | offset17 u32BE | offset21 u32BE | offset25 u32BE | offset29 u16BE count
    hdr = b''
    total = 31 + len(msg)
    hdr += struct.pack('>I', total)                    # offset0 frame length
    hdr += struct.pack('>I', 0) + struct.pack('>I', 0) + b'\x00'  # offset4 u32BE, offset8 u32BE + 1B
    hdr += struct.pack('>I', 0) + struct.pack('>I', 0) + struct.pack('>I', 0) + struct.pack('>I', 0)  # offset13,17,21,25
    hdr += struct.pack('>H', count)                    # offset29 count
    assert len(hdr) == 31, 'hdr=%d' % len(hdr)
    return hdr + msg

# ============================================================================
# ★★2026-10-12 (多人单服 · 逐条响应诊断): **唯一发送出口 + 结构化事件**
#
# 为什么必须"一个出口"：改造前出站字节有 **80+ 个发送点**（framing-A 里 60 多处
# `conn.sendall(pack_login_frame(...))`、LE 路径的 `send_frame`、`push_all/push_account`、
# `_push_chat_frame`、`handlers_point` 的两条红点推送），每处各写一份 `log('sent msg…')`，
# 而且长度是"帧长"不是"体长"、失败被 `if r:` 静默吞掉。于是"客户端说没收到某条响应"
# 时无法回答"发了没、发了什么、写成功没"。
#
# 做法：所有写仍然走 `_LockedConn.sendall`（它本来就是唯一持有连接写锁的地方 ——
# 业务线程与 HTTP 后台线程并发写同一个 socket 时必须串行），我们在**这一个点**上
# 记账，发送行为**逐字节不变**。业务侧只需在分发前 `WIRE.stage(...)` 声明
# "接下来这一帧是给谁的应答"，之后的 `emit()` 就能把"请求关联号/消息号/耗时/结果码"
# 补齐 —— 而**不需要**改那 80 个发送点。
#
# 三类事件（`kind`）：
#   · `resp` —— 由 `stage(kind='resp')` 声明后的**第一帧**（应答本身）
#   · `push` —— 同一个请求周期里后续的帧（如 1004 之后补推的 1077 聊天历史）
#             或任何没有 `stage` 的帧（`push_all`/`push_account`/红点）
#   · `deny` —— 由 `stage(kind='deny')` 声明的拒绝帧
# `begin_request()` 把请求登记进"待应答"账，`stage()`/`end_request()` 收账：
#   · 声明了 `resp` 却**一帧都没发** → 记一条 `unanswered`（漏记的机器可读证据）
#   · 没声明就发帧 → 记 `push`（不会伪装成应答）
# ============================================================================
class _WireRecorder(object):
    """把"下一次 sendall 是什么"这件事，从业务代码的 80 处收敛成**一处状态**。

    只做记账，绝不改动字节、绝不吞异常、绝不在失败路径上抛业务异常
    （诊断是旁路：它坏了不能把游戏弄坏）。
    """

    LE_MAGIC = b'\x00\x01\x01\x01'      # LE 路径不在此处记账（见 send_frame 的说明）

    def __init__(self):
        self._tls = threading.local()
        self._sha_cache = {}
        self._sha_lock = threading.Lock()
        self._sha_hits = 0
        self._sha_miss = 0

    # ---- 请求周期 ----
    def begin_request(self, conn, mid, corr=None, body=None, seq=None):
        st = self._state()
        diag = ygzz_diag.get()
        st['ctx'] = None
        st['kind'] = None
        st['reason'] = ''
        st['stopped'] = False
        if diag is not None:
            aid = getattr(conn, 'account_id', None)
            st['ctx'] = diag.begin_request(
                conn_id=_conn_id(conn), mid=mid, corr=corr, seq=seq, body=body,
                aid=aid, peer=_conn_peer(conn),
                frame_mode=getattr(conn, 'frame_mode', None))
        return st['ctx']

    def stage(self, kind, reason='', mid=None, corr=None):
        """声明"接下来要发的帧"的类别。`kind` ∈ resp/push/deny/none。"""
        st = self._state()
        st['kind'] = kind
        st['reason'] = reason
        st['stage_mid'] = mid
        st['stage_corr'] = corr

    def end_request(self, ok=True):
        """收账：声明了 `resp` 却一帧没发 → 记 `unanswered`（漏记的证据）。

        **幂等**：LE 路径的循环末尾与最外层 finally 都会调它，重复调用不得重复记账
        （否则同一帧会记出两条 `unanswered`，把"漏记的证据"变成噪声）。
        """
        st = self._state()
        ctx = st.get('ctx')
        kind = st.get('kind')
        if ctx is None and kind is None:
            return                       # 已经收过账 / 本轮根本没开始
        sent = int(st.get('sent', 0) or 0)
        if ctx is not None and kind == 'resp' and sent == 0:
            diag = ygzz_diag.get()
            if diag is not None:
                diag._record({
                    'ts': ygzz_diag._iso(), 't': round(time.time(), 3),
                    'kind': 'unanswered', 'conn': ctx.get('conn'), 'aid': ctx.get('aid'),
                    'acct': ctx.get('aid'), 'mid': ctx.get('req_mid'),
                    'corr': ctx.get('corr'), 'ok': False, 'bytes': 0,
                    'note': '分发了请求但一帧都没发',
                })
                diag._bump('unanswered')
        # 请求上下文必须显式作废：否则同一线程处理下一个连接时会误用旧身份。
        st['ctx'] = None
        st['kind'] = None
        st['reason'] = ''
        st['sent'] = 0
        st['stopped'] = False

    # ---- 记账（由 _LockedConn.sendall 调用）----
    def note(self, conn, data, ok, err, mid=None, seq=None):
        st = self._state()
        ctx = st.get('ctx')
        kind = st.get('kind')
        diag = ygzz_diag.get()
        if diag is None or not diag.enabled:
            return
        st['sent'] = int(st.get('sent', 0) or 0) + 1
        try:
            self._note_locked(conn, data, ok, err, mid, seq, st, ctx, kind, diag)
        except Exception as _e:
            # ★★ 诊断是旁路，**不能**把业务弄坏；但也**不能**静默 —— 静默过的代价
            #   本轮实测到了：替身少一个成员就抛 AttributeError，被吞掉之后症状是
            #   "响应根本没发出去"（测试里表现为 `0 != 1`），而真正的原因一个字都没有。
            #   ⇒ 记进 counters（`/admin` 的 summary 里看得到）+ 让 `dbg` 留一行。
            self._internal_errors = getattr(self, '_internal_errors', 0) + 1
            try:
                diag._bump('wire_internal_error')
                diag._counters['wire_internal_error_last'] = repr(_e)[:200]
            except Exception:
                pass
            try:
                dbg('WIRE note 内部异常（诊断旁路，业务未受影响）：%r' % (_e,))
            except Exception:
                pass

    def _note_locked(self, conn, data, ok, err, mid, seq, st, ctx, kind, diag):
        # ⚠ 第一帧之后一律算推送：一个请求周期里"应答"只有一条，
        #   后续帧（1077/1059/1046/8081/8082…）都是推送。这条判据就是
        #   "无重复"的机器保证 —— 重复 emit 同一条应答在账上是不可能的。
        if kind == 'none':
            st['sent'] = int(st.get('sent', 0) or 0) - 1   # 显式关账的帧不占应答名额
            return
        if kind == 'resp' and st['sent'] == 1:
            good = True
        elif kind == 'deny':
            good = True
        else:
            good = False
        fm = None if mid is not None else getattr(conn, 'frame_mode', None)
        fmid, fseq = _frame_ids(data, fm)
        if mid is None:
            mid = fmid
        if seq is None:
            seq = fseq
        nbytes = len(data) if isinstance(data, (bytes, bytearray)) else 0
        sha = self._sha(data)
        if good:
            if ctx is not None:
                # 走 Diag 的正式应答路径：它会把这条从"待应答"账上摘掉，
                # 摘不掉就抛 DiagError（= 重复应答），由调用方转成 dup_resp 证据。
                # ★ 字节数 / 内容哈希 / 结果码**在这里落进 ctx**：哈希刚在 `_sha()` 里
                #   算过一遍，交给 `emit_resp` 直接用 —— 否则日志里会留一个
                #   `bytes=0`（看不出"真没发"还是"没记到"）的值。
                try:
                    ctx['resp_bytes'] = nbytes
                    ctx['resp_sha'] = sha
                    ctx['resp_code'] = ygzz_diag.result_code(data)
                except Exception:
                    pass
                try:
                    diag.emit_resp(ctx, mid, None, ok=ok, err=err,
                                   note=st.get('reason') or kind)
                except Exception as e:
                    diag._record({
                        'ts': ygzz_diag._iso(), 't': round(time.time(), 3),
                        'kind': 'dup_resp', 'conn': ctx.get('conn'),
                        'aid': ctx.get('aid'), 'acct': ctx.get('aid'),
                        'mid': mid, 'corr': ctx.get('corr'), 'ok': False,
                        'bytes': nbytes, 'err': repr(e)[:240],
                        'note': '同一条请求发了两帧应答',
                    })
                    diag._bump('dup_resp')
            else:
                # 没有请求上下文却声明了 resp（例如鉴权拒绝发生在分帧之前）：
                # 记成 deny/push 也**不能丢** —— 出站字节必须有账。
                diag.emit_push(_conn_id(conn), getattr(conn, 'account_id', None),
                               mid, None, ok=ok, err=err,
                               reason=st.get('reason') or 'resp-无请求上下文',
                               via='wire')
            _recount(diag, nbytes, sha)
            return
        # 推送 / 后续帧
        aid = getattr(conn, 'account_id', None)
        if ctx is not None and ctx.get('aid') is not None:
            aid = ctx.get('aid')
        diag.emit_push(_conn_id(conn), aid, mid, None, ok=ok, err=err,
                       reason=st.get('reason') or ('同一请求周期的后续帧'
                                                   if kind else '无请求上下文'),
                       via='wire')
        _recount(diag, nbytes, sha)

    def _state(self):
        st = getattr(self._tls, 'st', None)
        if st is None:
            st = {'ctx': None, 'kind': None, 'reason': '', 'sent': 0, 'stopped': False}
            self._tls.st = st
        return st

    def _sha(self, data):
        """内容哈希。**带缓存**：静态帧（1031/1032/1506 等）在整局里内容不变，
        每次都重算 sha256 是纯浪费（大背包帧 50 KB+）。缓存键就是字节串本身。"""
        if not isinstance(data, (bytes, bytearray)):
            return ''
        key = bytes(data)
        with self._sha_lock:
            v = self._sha_cache.get(key)
            if v is not None:
                self._sha_hits += 1
                return v
        v = ygzz_diag.sha16(key)
        with self._sha_lock:
            if len(self._sha_cache) > 4096:
                self._sha_cache.clear()
            self._sha_cache[key] = v
            self._sha_miss += 1
        return v

    def stats(self):
        with self._sha_lock:
            return {'sha_cache': len(self._sha_cache), 'sha_hits': self._sha_hits,
                    'sha_miss': self._sha_miss}


# ---- 鉴权门：未认证（未带有效票据）的连接只准走登录链 ------------------------------
# 依据（客户端 `SocketManager.lua` / `MLoginHome.lua` 的实际顺序）：
#   连上 -> 1007 SDK_LOGIN -> 1000 HEARTBEAT -> 1005 GAME_SERVER -> 1004 ROLE_INFO
# （建角时在 1004 之前多一条 1002 ROLE_CREATE + 1009 ROLE_NAME）。
# 这几个号**都不读也不写玩家业务档**：1000/1005 是纯 ack/地址下发，1003/1007 是
# 登录握手，1004 是"带票进来"的那一刻。其余一律拒绝。
# ★★2026-10-26（审查 H1，**已从白名单移除 1002**）：`ROLE_CREATE` 会写 `role_name`
#   并落盘，把它留在白名单里等于"未认证也能写档"（审查复现：1002 直接改 IP 映射档的
#   角色名）。现在 1002 必须先携票（`_bind_ticket_if_authed` 在 `_gate_ok` **之前**跑），
#   通过后 `conn.authed=True` 才放行；不带票 → 1013 + 结束连接。
_PREAUTH_MSGS = frozenset({1000, 1003, 1004, 1005, 1007, 1008, 1009})


def _auth_active():
    """鉴权是否**真正生效**：开关开着 **且** 注册表已建。

    ⚠ 为什么必须两条件一起判（而不是只判 `AUTH_ENABLED`）：`AUTH` 由
      `__main__` 段的 `auth_enable()` 建；测试/工具进程只执行模块级代码 ⇒
      `AUTH_ENABLED=True` 而 `AUTH is None` 是一种**真实存在的状态**。
      那种状态下只判开关，就会把"没有票据校验能力"当成"票据校验失败" ——
      所有连接被判 1013，而日志里只有一行语焉不详的拒绝。
      （实测：`_account_index_safety_test` 的 8 条用例就是这么一起红的。）
    """
    return bool(AUTH_ENABLED and AUTH is not None)


def _gate_ok(conn, mid):
    """鉴权门。返回 `(允许?, 原因)`。

    未启用鉴权时**恒允许**（默认路径逐字节不变）—— 这是回归网 121 套依赖的语义。
    """
    if not _auth_active():
        return True, ''
    if conn.authed:
        return True, ''
    if mid in _PREAUTH_MSGS:
        return True, ''
    return False, 'preauth'


WIRE = _WireRecorder()


def _recount(diag, nbytes, sha):
    """把字节数/哈希累进 counters（`def summary()` 用来回答"这段时间发出去了多少"）。"""
    c = diag._counters
    c['bytes_out'] = c.get('bytes_out', 0) + int(nbytes or 0)
    c['frames_out'] = c.get('frames_out', 0) + 1
    if sha:
        # 不存哈希本身（内存），只留"有多少个不同的内容"这个量 —— 它足以回答
        # "是不是每条响应都一样"（= 是不是兜底体）这类问题。
        # ⚠ 上限 4096：内容哈希的集合会随"新内容"无界增长，那也是内存泄漏。
        s = c.get('sha_seen')
        if not isinstance(s, set):
            s = c['sha_seen'] = set()
        if len(s) < 4096:
            s.add(sha)
        else:
            c['sha_seen_truncated'] = c.get('sha_seen_truncated', 0) + 1


def _conn_id(conn):
    """连接标识：**fd + 建立序号**。只用 fd 会被复用（关掉的 12 号 fd 立刻给新连接）。"""
    v = getattr(conn, 'conn_seq', None)
    if v is None:
        with _LIVE_LOCK:
            _CONN_SEQ[0] += 1
            v = _CONN_SEQ[0]
        try:
            conn.conn_seq = v
        except Exception:
            v = 0
    return 'c%05d/fd%s' % (v, getattr(conn, '_live_fd', '?'))


def _conn_peer(conn):
    a = getattr(conn, 'addr', None)
    if isinstance(a, tuple) and len(a) >= 2:
        return '%s:%s' % (a[0], a[1])
    return None


_CONN_SEQ = [0]


def _frame_ids(data, mode):
    """从**已封好的帧字节**里取 (msgid, seqid)，用来给推送记账。

    帧格式（`pack_login_frame`）: [31B 头][u32BE 10000][u32BE msgid][u32BE bodysize][body]
        → msgid 在偏移 8
    LE 帧（`send_frame`）: [u32LE total][u32LE msgid][u32LE seqid][base64 json]
        → msgid 在偏移 4
    取不到就回 `(None, None)` —— **不猜**。
    """
    if not isinstance(data, (bytes, bytearray)):
        return None, None
    try:
        if mode == 'LE' and len(data) >= 12:
            _t, mid, seq = struct.unpack('<III', bytes(data[:12]))
            return mid, seq
        if len(data) >= 40:
            tag = struct.unpack('>I', bytes(data[31:35]))[0]
            if tag == 10000:
                return struct.unpack('>I', bytes(data[35:39]))[0], 0
        if len(data) >= 12:
            _t, mid, seq = struct.unpack('<III', bytes(data[:12]))
            return mid, seq
    except Exception:
        pass
    return None, None


def push_skill_bag(conn):
    # 推送技能袋 (1507 SKILL_RECV; NetSkillRecv:recvMessage 增量 insertItem, 幂等)
    #   抽卡/碎片合成/礼包/进化/开箱等新增怪物后, 客户端 SQSkill 缺原生技能条目 ->
    #   怪面板技能显示 "Dummy" (2026-09-07 神授案例: L91 技能行id有效但客户端技能袋无此 ucode)
    try:
        from handlers_draw import skill_bag_json
        conn.sendall(pack_login_frame(1507, 0, skill_bag_json().encode('utf-8')))
    except Exception as e:
        log('push 1507 err %r' % e)

def push_prop_bag(conn):
    # 推送道具背包 (1033 PROP_BAG; NetPropBag:recvMessage page=1 -> prop_deleteTotalItem + 全量 insert, 幂等)
    #   1404 兑换商城购买把 1039/1040 八卦币扣走后, 客户端 SQProp:prop_getCount(1039) 只增不减
    #   (VSweepstakeShop:L57/58 货币栏直接读 SQProp 计数) -> 购买成功后补推全量背包 (2026-09-07 v10 §2.4)
    try:
        conn.sendall(pack_login_frame(1033, 0, prop_bag_json().encode('utf-8')))
    except Exception as e:
        log('push 1033 err %r' % e)

def role_info_json():
    # ROLE_INFO(1004)/GET_VITALITY(3002) 动态 roleInfo。
    # ★2026-09-13 (v32) 收敛: 这里原本是**第三份** roleInfo 30 字段构造代码
    #   (另两份在 handlers_sys / handlers_bag, 字段还不一致 -> 见 state.role_info_array 的复盘)。
    #   字段布局/语义/坑点全部搬到 state.role_info_array(), 此处只做响应包裹。
    #   客户端 NetRoleInfo: roleInfo = msg[1] -> 需要外层 [1,[ <30字段数组> ]]
    return '[1,[' + role_info_array() + ']]'

# --- 登录读取器(ToLua CSys* 函数)真实格式 (Ghidra g_sock_createrole.c 确认) ---
# SocketInputStream::Read(u16 len, 字节交换) 然后 Read(len) -> string(json), 再 Read(u32), Read(u32)
# 即响应 = [u16BE len][json 字符串][u32 msgid][u32 seqid]
def build_login_frame(json_str, msgid=1007, seqid=0):
    b = json_str
    return struct.pack('>H', len(b)) + b + struct.pack('>I', msgid) + struct.pack('>I', seqid)
def build_cg_response(raw, new_json):
    # class=10000(generic CGMessage) + logical msgid=1007(SDK_LOGIN) — pack_login_frame uses class=10000 + msgid
    return pack_login_frame(1007, 0, new_json)
def build_session_response(raw, new_json):
    return pack_login_frame(1008, 0, new_json)

def build_game_server_response(raw, new_json):
    return pack_login_frame(1005, 0, new_json)
def framing_a_msgid(raw):
    # 在客户端 framing A 帧里取 msgid(u32 BE @35-38)；取不到返回 None
    if len(raw) >= 39 and raw[:5] == b'\x00\x01\x01\x01\x00':
        return struct.unpack('>I', raw[35:39])[0]
    return None

def send_frame(conn, msgid, seqid, obj):
    body=json.dumps(obj,separators=(',',':')).encode('utf-8')
    b64=base64.b64encode(body)
    total=12+len(b64)
    # ★2026-10-12 (诊断): LE 帧的 msgid/seqid 就在**参数里**，不要从字节里反解 ——
    #   反解需要知道是哪种帧格式，而"猜帧格式"正是本项目最容易错的地方。显式传下去。
    conn.sendall(struct.pack('<III', total, msgid, seqid) + b64,
                 mid=msgid, seq=seqid)

def _push_mail_delayed(conn):
    try:
        conn.sendall(pack_login_frame(1059, 0, MAIL_TOTAL_JSON))
        log('pushed msg(1059) MAIL_TOTAL (delayed)')
    except Exception as e:
        log('push mail err %r' % e)

def letter_rev_json():
    # 1046 动态构造: 已读(id 在 letter_done) state=1, 未读 state=0
    #   形状依据客户端源码 (NetLetterRev:recvMessage): parseMessage 取响应最后元素为 msg,
    #   msg[1] 必须是 [id,state] 对的列表 -> 单封手札 = [1, [[[101,0]]]]
    done = set(str(x) for x in EQ_STATE.get('letter_done', []))
    pair = '[%s,%d]' % (101, 1 if '101' in done else 0)
    return ('[1,[[' + pair + ']]]').encode('utf-8')

def handle_letter_update(body_json):
    # 1047 LETTER_UPDATE: 客户端手札"已读"通知 (真机 16:53:58 确认客户端会发) -> 记录持久, 下次登录不再弹
    try:
        req = json.loads(body_json) if body_json else {}
        lid = int(req.get('id') or 0)
    except Exception:
        return b'[0,{}]'
    if lid > 0:
        lst = EQ_STATE.setdefault('letter_done', [])
        if lid not in lst:
            lst.append(lid)
        save_state()
    return b'[1,{}]'

FRAMING_A_MAGIC = b'\x00\x01\x01\x01\x00'

class _BufShim:
    """把"还没消费完的字节"接在 socket 前面 (只实现 read_frame 用到的 recv/settimeout/close)。
    用于 framing-A -> LE 路径切换时保住残留字节 (v32)。"""
    def __init__(self, sock, buf):
        self._s = sock
        self._b = buf

    def recv(self, n):
        if self._b:
            d, self._b = self._b[:n], self._b[n:]
            return d
        return self._s.recv(n)

    def settimeout(self, t):
        return self._s.settimeout(t)

    def close(self):
        return self._s.close()

def _read_framing_a_frame(conn, buf):
    """从 (buf + socket) 里**按帧长精确读满**一个 framing-A 请求帧。

    ★★2026-09-13 (v32) 为什么必须这么读 (两条都是实测坐实的故障, 见 work/_probe_framingA.py):
      旧写法 `raw = conn.recv(8192)` 把「一次 recv」当成了「一个请求帧」, 但 TCP 是**字节流**:
        ① 截断: 帧 > 8192 字节时第一次 recv 只拿到前半段 -> extract_body_json() 的
           `len(raw) >= 48+blen` 不成立 -> 返回 None -> handler 拿到 [0,{}]。
           实测: 3202 发 9015 字节 body -> 响应 [0,{}] (正常应为 [2001])。
        ② 粘包丢帧: 客户端一次 write 出两帧 -> 一次 recv 同时拿到两帧, 旧代码只处理第一帧,
           **同一 buffer 里的第二帧被整个丢弃**, 客户端只能等超时重发。
           实测: 一次 sendall(1004 + 1055) -> 只收到 1 个响应。
      帧长是**可精确算出**的: [0:5] magic, [46:48] u16BE body 长度, 总长 = 48 + blen。
      返回:
        (bytes, 剩余buf)  —— 读到一个完整帧
        ('LE', buf)       —— 不是 framing-A 帧 (交给后面的 LE/read_frame 路径)
        (None, buf)       —— 对端关闭 / 半包断开
        ('BIG', buf)      —— ★2026-10-12: 帧长超过上限（见下）
    """
    while True:
        if len(buf) < 5:
            d = conn.recv(65536)
            if not d:
                return None, buf
            buf += d
            conn.bytes_in = int(getattr(conn, 'bytes_in', 0)) + len(d)
            continue
        if not buf.startswith(FRAMING_A_MAGIC):
            return 'LE', buf
        if len(buf) < 48:
            d = conn.recv(65536)
            if not d:
                return None, buf
            buf += d
            conn.bytes_in = int(getattr(conn, 'bytes_in', 0)) + len(d)
            continue
        blen = struct.unpack('>H', buf[46:48])[0]
        need = 48 + blen
        if blen <= 0:
            # body 长度 0: 该帧只有 48 字节头 (1005/1000 这类无体请求走的是 LE 路径, 这里只做防御)
            return buf[:48], buf[48:]
        if len(buf) < need:
            # ★★2026-10-12 (限额): 单帧硬上限。
            #   本帧格式的 body 长度是 **u16BE** ⇒ 单帧最大 65583 字节，本来就有天花板；
            #   但**累积缓冲**没有 —— 对端只要持续发不含 magic 的垃圾（或宣布一个刚好
            #   读不满的长度），`buf` 就会一直长，直到把内存吃光。这是"限制帧长与请求体"
            #   要挡的那一类：不是畸形包，而是**合法形状的慢速灌入**。
            if len(buf) > MAX_FRAME_BYTES:
                return 'BIG', buf
            d = conn.recv(65536)
            if not d:
                return None, buf
            buf += d
            conn.bytes_in = int(getattr(conn, 'bytes_in', 0)) + len(d)
            continue
        if need > MAX_FRAME_BYTES:
            return 'BIG', buf
        return buf[:need], buf[need:]

def _account_guard_call(fn, *args):
    """多账号模式下把普通处理器与妖盟事务放在同一请求级临界区。"""
    reg = accounts.REGISTRY if accounts.ENABLED else None
    if reg is None:
        return fn(*args)
    with reg._guild_lock:
        return fn(*args)


def handle_game(conn, addr, port=0):
    log('GAME connect port=%s %s' % (port, addr))
    conn.settimeout(30)
    # Login socket uses framing A (00 01 01 01). Keep the connection alive and answer 1007/1008/etc.
    # ★v32: buf 是**连接级残留缓冲** —— 一次 recv 拿到的多余字节必须留到下一轮, 否则会丢帧。
    buf = b''
    identity_failed = False
    try:
        while True:
            request_lock = None
            request_locked = False
            try:
                raw, buf = _read_framing_a_frame(conn, buf)
                if raw is None:
                    log('GAME login conn closed')
                    break
                # ★2026-10-12: 身份已经确认失败过就**不要再处理后续请求**。
                #   虽然每条身份失败路径都会 `break`，但"兜底帧"路径（per-request
                #   `except`）会 `continue` —— 那种情况下接着处理下一帧，等于让一条
                #   身份不明的连接继续驱动业务逻辑。这里是结构性护栏，不依赖各分支自觉。
                if identity_failed:
                    log('GAME 身份已失败 —— 结束连接（不再处理后续帧）')
                    break
                if raw == 'LE':
                    conn.frame_mode = 'LE'
                    break                       # 不是 framing-A -> 交给后面的 LE 路径
                if raw == 'BIG':
                    # ★2026-10-12 (限额): 帧长越界 -> **拒绝并结束连接**，不留半截缓冲。
                    log('GAME 帧长越界(>%d) 断开 %s conn=%s' % (MAX_FRAME_BYTES, addr,
                                                              _conn_id(conn)))
                    DIAG.emit_deny(_conn_id(conn), getattr(conn, 'account_id', None), None,
                                   None, 'frame_too_big', code=None, ok=False,
                                   err='frame > %d' % MAX_FRAME_BYTES)
                    break
                conn.frame_mode = 'A'
                _t0 = time.time()
                mid = struct.unpack('>I', raw[35:39])[0]
                # ★2026-10-12 (限额): 单连接请求数 / 累计入站字节。
                #   为什么必须有：登录连接是**长连接**（客户端 tick 会一直发），一条被脚本
                #   驱动的连接可以在几分钟内打出几十万条请求 —— 它不是攻击特征（形状完全
                #   合法），但会把单线程的 `_guild_lock` 与 state 落盘拖死。
                conn.reqs = int(getattr(conn, 'reqs', 0)) + 1
                if conn.reqs > MAX_REQS_PER_CONN or int(getattr(conn, 'bytes_in', 0)) > MAX_BYTES_PER_CONN:
                    log('GAME 连接超限 reqs=%d bytes_in=%d conn=%s -> 断开'
                        % (conn.reqs, int(getattr(conn, 'bytes_in', 0)), _conn_id(conn)))
                    DIAG.emit_deny(_conn_id(conn), getattr(conn, 'account_id', None), mid,
                                   None, 'conn_quota', code=None, ok=False,
                                   err='reqs=%d bytes_in=%d' % (conn.reqs,
                                                                int(getattr(conn, 'bytes_in', 0))))
                    break
                # ★★2026-10-12 (诊断): 登记"我收到了这条请求"。之后这个线程里
                #   发出的**第一帧**自动成为它的应答；`stage('push')` 只用于
                #   "这个号本来就没有应答"的分支（103 之类）。
                WIRE.begin_request(conn, mid, corr=None, seq=0, body=None)
                WIRE.stage('resp', reason='framing-A 分支应答')
                dbg('FRAMING_A msgid=%s' % mid)
                log('recv msgid=%s' % mid)
                # ★★2026-10-26（审查 H1）：1002 ROLE_CREATE 会写 `role_name` 并落盘，
                #   而它过去在 `_PREAUTH_MSGS` 白名单里 ⇒ 未认证连接也能写档（还写的是
                #   本连接按来源 IP 映射的档）。现在它不在白名单里，且必须在**门之前**
                #   用票据完成身份绑定：不带票/错票 → AccountBindingError → 1013 + 断开。
                if mid == MSG['ROLE_CREATE'] and _auth_active() and not conn.authed:
                    _bind_userid_if_real(extract_body_json(raw), conn)
                # ★★2026-10-12 (鉴权门): 未认证的连接只允许"登录链"消息。
                #   为什么要有这道门：鉴权发生在 1004（客户端到那时才把服务端签发的
                #   userid 送回来），而 1004 之前客户端还会发 1007/1005/1000。若把门
                #   开在连接建立时，登录链自己就走不通；若完全不开，未认证连接就能
                #   调任意 handler（包括会落盘的）。⇒ 白名单只放"不读不写业务档"的那几个。
                _allowed, _why = _gate_ok(conn, mid)
                if not _allowed:
                    log('AUTH 未认证连接请求 msg(%s) 被拒 conn=%s' % (mid, _conn_id(conn)))
                    DIAG.emit_deny(_conn_id(conn), None, mid, b'[1013]', _why,
                                   code=ygzz_auth.SOCKET_BAD_TICKET, ok=True)
                    WIRE.stage('deny', reason='鉴权门拒绝')
                    try:
                        conn.sendall(pack_login_frame(
                            mid, 0, ('[%d]' % ygzz_auth.SOCKET_BAD_TICKET).encode('utf-8')))
                    except Exception:
                        pass
                    continue
                if accounts.ENABLED and accounts.REGISTRY is not None:
                    request_lock = accounts.REGISTRY._guild_lock
                    request_lock.acquire()
                    request_locked = True
                if mid == MSG['CULTIVATE_INTENSITY']:  # 6006 装备强化
                    body_json = extract_body_json(raw)
                    resp = handle_equip_intensity(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(%s) equip_intensity resp n=%d body=%s' % (mid, len(r), resp[:120]))
                elif mid == MSG['CULTIVATE_LOAD']:  # 6004 装备穿戴
                    body_json = extract_body_json(raw)
                    # ★v34: 帧体取不到时不再回 [0,{}] ("错误，消息体为空"); 交给 handler 走
                    #   json.loads(None) -> except -> [70] 要上装的怪物为空 (客户端有对应文案)
                    resp = handle_equip_load(body_json)
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(%s) equip_load resp n=%d body=%s' % (mid, len(r), resp[:120]))
                elif mid == MSG['CULTIVATE_UNLOAD']:  # 6005 装备卸下
                    body_json = extract_body_json(raw)
                    resp = handle_equip_unload(body_json)
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(%s) equip_unload resp n=%d body=%s' % (mid, len(r), resp[:120]))
                elif mid == MSG['DRAW_CARD']:  # 8002 抽卡 (真抽: 扣费+加权+进背包)
                    body_json = extract_body_json(raw)
                    resp = handle_draw_card(body_json) if body_json is not None else b'[80]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(8002) draw_card resp n=%d req=%s body=%s' % (len(r), (body_json or '')[:80], resp[:160]))
                    push_skill_bag(conn)   # 抽卡给新怪 -> 客户端技能袋缺原生技能会显示 Dummy (神授案例)
                elif mid == 3002:  # GET_VITALITY: 恢复核准 + roleInfo + 补推 1077 倒计时
                    body_json = extract_body_json(raw)
                    resp = handle_get_vitality(body_json) if body_json is not None else b'[3001]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r)
                    conn.sendall(pack_login_frame(1077, 0, vitality_remain_push()))
                    log('sent msg(3002) get_vitality + push 1077 n=%d' % len(r))
                elif mid in (MSG['ROLE_INFO'], 1040):  # ROLE_INFO/UPDATE_INFO -> 动态金币 roleInfo (避免客户端把金币覆盖成0)
                    # ★2026-10-02 (任务 F / D3=C): 客户端在 1004 的 body 里自报 userid，
                    #   必须在**生成 roleInfo 之前**改绑 —— roleInfo 是读当前账号上下文的。
                    if mid == MSG['ROLE_INFO']:
                        _bind_userid_if_real(extract_body_json(raw), conn)
                    r = pack_login_frame(mid, 0, role_info_json().encode('utf-8'))
                    if r: conn.sendall(r); log('sent msg(%s) role_info resp n=%d' % (mid, len(r)))
                    if mid == MSG['ROLE_INFO'] and accounts.ENABLED:
                        _push_chat_history(conn)
                elif mid == 8001:  # 8001 进入抽卡界面 (动态券/幸运/保底)
                    resp = handle_card_time()
                    conn.sendall(pack_login_frame(mid, 0, resp)); log('sent msg(8001) card_time resp n=%d body=%s' % (len(resp), resp[:120]))
                elif mid == 1076:  # VITALITY_PUSH_BEGIN: ack + 推 1077 恢复倒计时 (客户端借此启动本地定时器)
                    resp = handle_vitality_push_begin()
                    conn.sendall(pack_login_frame(mid, 0, resp))
                    conn.sendall(pack_login_frame(1077, 0, vitality_remain_push()))
                    log('sent msg(1076) push_begin + push 1077 countdown')
                elif mid == 1032:  # MONSTER_BAG 动态 (含抽到的怪)
                    resp = monster_bag_json().encode('utf-8')
                    conn.sendall(pack_login_frame(mid, 0, resp)); log('sent msg(1032) monster_bag dyn n=%d' % len(resp))
                elif mid == 1030:  # EQUIP_BAG 动态 (反映真实 isload/等级, 避免"已装备但没穿上/等级都1")
                    resp = equip_bag_json().encode('utf-8')
                    conn.sendall(pack_login_frame(mid, 0, resp)); log('sent msg(1030) equip_bag dyn n=%d' % len(resp))
                elif mid == 1506:  # SKILL_BAG 动态 (含抽到怪的技能)
                    resp = skill_bag_json().encode('utf-8')
                    conn.sendall(pack_login_frame(mid, 0, resp)); log('sent msg(1506) skill_bag dyn n=%d' % len(resp))
                elif mid == 1033:  # PROP_BAG 动态 (真实数量/道具, 含合成碎片)
                    resp = prop_bag_json().encode('utf-8')
                    conn.sendall(pack_login_frame(mid, 0, resp)); log('sent msg(1033) prop_bag dyn n=%d' % len(resp))
                elif mid == 1031:  # GHOST_BAG 动态 (元灵: [ucode,id,count,isload])
                    resp = ghost_bag_json().encode('utf-8')
                    conn.sendall(pack_login_frame(mid, 0, resp)); log('sent msg(1031) ghost_bag dyn n=%d' % len(resp))
                elif mid == MSG['BAG_SELL']:  # 1178 出售 (道具/装备)
                    body_json = extract_body_json(raw)
                    resp = handle_bag_sell(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1178) bag_sell resp n=%d body=%s' % (len(r), resp[:160]))
                elif mid == MSG['BAG_USE']:  # 1179 使用礼包
                    body_json = extract_body_json(raw)
                    dbg('1179 req=%s' % (body_json or ''))
                    resp = handle_bag_use(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1179) bag_use resp n=%d body=%s' % (len(r), resp[:160]))
                elif mid == MSG['GIFT_BAG_USE']:  # 1098 使用周卡/月卡
                    body_json = extract_body_json(raw)
                    resp = handle_gift_bag_use(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1098) gift_bag_use resp n=%d body=%s' % (len(r), resp[:160]))
                elif mid == MSG['BAG_COMPOSE']:  # 1177 背包合成 (元灵/技能碎片)
                    body_json = extract_body_json(raw)
                    resp = handle_bag_compose(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1177) bag_compose resp n=%d body=%s' % (len(r), resp[:160]))
                elif mid == MSG['PIECE_COMPOSE']:  # 1168 碎片合成 (妖怪/皮肤/其他)
                    body_json = extract_body_json(raw)
                    resp = handle_piece_compose(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1168) piece_compose resp n=%d body=%s' % (len(r), resp[:160]))
                    push_skill_bag(conn)
                elif mid == MSG['GIFT_BAG_GET']:  # 1097 礼包/周卡月卡列表 (无请求体)
                    resp = gift_bag_get_json().encode('utf-8')
                    conn.sendall(pack_login_frame(mid, 0, resp)); log('sent msg(1097) gift_bag_get n=%d body=%s' % (len(resp), resp[:120]))
                elif mid == 6002:  # 吞噬(养成) 成长值+属性加成+经验升级 (对标数据)
                    body_json = extract_body_json(raw)
                    dbg('6002 req=%s' % (body_json or ''))
                    resp = handle_monster_devour(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(6002) monster_devour resp n=%d body=%s' % (len(r), resp[:220]))
                elif mid == MSG['SKIN_GET']:  # 1500 皮肤列表
                    body_json = extract_body_json(raw)
                    resp = handle_skin_get(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1500) skin_get n=%d' % len(r))
                elif mid == MSG['SKIN_REPLACE']:  # 1501 替换皮肤
                    body_json = extract_body_json(raw)
                    resp = handle_skin_replace(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1501) skin_replace n=%d' % len(r))
                elif mid == MSG['FORMATION_LIST']:  # 3200 布阵
                    body_json = extract_body_json(raw)
                    dbg('3200 req=%s' % (body_json or ''))
                    resp = handle_formation_list(body_json)
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(3200) formation_list n=%d' % len(r))
                elif mid == MSG['FORMATION_UPDATE']:  # 3201 上阵/下阵
                    body_json = extract_body_json(raw)
                    dbg('3201 req=%s' % (body_json or ''))
                    resp = handle_formation_update(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(3201) formation_update n=%d' % len(r))
                elif mid == MSG['FORMATION_SAVA']:  # 3202 布阵保存 —— ★开战回调的唯一触发点
                    # ★2026-09-12 (v26 P0): 此前无此分支 -> 落 resp_for 兜底 [1,[[]]] ->
                    #   NetFormationSave.lua:101 `pairs(formation=msg[2]=nil)` 抛错 ->
                    #   m_callFunc 永不执行 -> WarStagePVE 的 netCallFunc(startFight+startBattle) 永不触发
                    #   -> "点开战进不去战斗" + socket tick 中断导致 74 秒重试 84 次。
                    body_json = extract_body_json(raw)
                    dbg('3202 req=%s' % (body_json or ''))
                    resp = handle_formation_save(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(3202) formation_save n=%d body=%s' % (len(r), resp[:200]))
                elif mid == MSG['MAIL_RECEIVE']:  # 1057 领取邮件奖励
                    body_json = extract_body_json(raw)
                    resp = handle_mail_receive(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1057) mail_receive n=%d' % len(r))
                elif mid == MSG['MAIL_DEL']:  # 1056 删除邮件 (单个/一键)
                    # ★2026-09-12 (v26 goal-round-5): 此前无处理 -> 兜底 [1,[[]]] ->
                    #   NetMailDelete `pairs(msg[1])` 收到空表 -> 不崩, 但**列表不刷新** =
                    #   "点了删除没反应"。调用点 MailList.lua:291(单个)/456(一键)。
                    body_json = extract_body_json(raw)
                    resp = handle_mail_del(body_json) if body_json is not None else b'[0,{}]'
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1056) mail_del n=%d body=%s' % (len(r), resp[:120]))
                elif mid == MSG['MAIL_GET']:  # 1055 邮件列表 (并推送1059)
                    resp = handle_mail_get().decode('utf-8').encode('utf-8')
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(1055) mail_get n=%d' % len(r))
                    conn.sendall(pack_login_frame(1059, 0, handle_mail_total())); log('pushed msg(1059) MAIL_TOTAL')
                elif mid == 103:  # PUSH_LETTER 新手手札: 回确认 + 【主动推 1046 手札列表】
                    # 源码核实: 客户端从不发 1046; NetPushLetter:recvMessage 只打日志不填列表;
                    #   列表只能由服务器推 -> 否则 TipsManager:checkLetter 每 tick 重发 103 (日志实测 994 次)
                    r = pack_login_frame(mid, 0, b'[1,{}]')
                    if r: conn.sendall(r)
                    conn.sendall(pack_login_frame(1046, 0, letter_rev_json()))
                    log('sent msg(103) push_letter ack + pushed msg(1046) LETTER_REV')
                elif mid == 1047:  # LETTER_UPDATE 手札已读 -> 持久 (下次推送 state=1, 不再弹)
                    resp = handle_letter_update(extract_body_json(raw))
                    r = pack_login_frame(mid, 0, resp if resp else b'[1,{}]')
                    if r: conn.sendall(r); log('sent msg(1047) letter_update n=%d' % len(r))
                else:
                    _t_req = time.time()
                    # 修复: 旧代码在该分支会 SEND 两次 (先 in-branch 发, 外层再发) -> 重复响应
                    _h = GAME_HANDLERS.get(mid)
                    if _h:
                        _fn, _nb = _h
                        if _nb:
                            body_json = extract_body_json(raw)
                            # ★v54b (2026-09-15): 战斗结算类请求体留痕。
                            #   原因: 这些协议把**官方掉落组 id** 加密后放在 value/evalue 里
                            #     (NetWarResult.lua:41 / NetTowerResult / NetBossResult / NetPVEResult /
                            #      NetCardBossResult 用 ZZRc4.encodeEncrypt, NetAggressResult 的 evalue 是明文),
                            #     而 time 同包下发 -> 服务端现在有 zzrc4.py 可以解。
                            #   留痕是为了: ①用真样本交叉验证解出来的 id 是否落在 T02_Reward.GroupID / T01 表内;
                            #              ②以后接 BattleReward 时不必再重抓。截断 400 字符, 单体量很小。
                            if mid in (9001, 9002, 9003, 9004, 9005, 9006, 9007, 8006, 1704, 1702, 1034, 1311, 1312, 1313, 4443, 4444, 4445):
                                dbg('%s req=%s' % (mid, (body_json or '')[:4000]))
                            resp = _fn(body_json) if body_json is not None else b'[0,{}]'
                        else:
                            resp = _fn()
                    else:
                        resp = resp_for(mid)
                    r = pack_login_frame(mid, 0, resp)
                    if r: conn.sendall(r); log('sent msg(%s) response n=%d' % (mid, len(r)))
                    # ★v57 诊断: 6003 进化的实际响应体留痕(排查"进化不刷新")
                    if mid == 6003:
                        dbg('EVOLVE resp raw=%r' % (resp[:400] if isinstance(resp, bytes) else str(resp)[:400]))
                    # ★2026-09-12 (v26 P2) 聊天: 8080 发出成功后**主动推 8081 增量**,
                    #   否则玩家发了消息自己屏幕上看不到 (客户端 ChatWorld/ChatPrivate 只 append 收到的推送)。
                    #   ⚠ 只推**最后一条**: NetChatTotal:recvMessage 是 `table.insert(m_worldMsg, ...)` 累加语义,
                    #     推全量会让列表重复 N 倍 (见交接档 §5 坑点 #41)。
                    if mid == 8080 and resp[:2] == b'[1':
                        log('pushed msg(8081) CHAT_TOTAL delta connections=%d' % _push_chat_delta(conn))
                    _el = time.time() - _t_req
                    if _el > 0.1:
                        dbg('SLOW msg(%s) %.3fs' % (mid, _el))
                    if mid in (1078, 1089, 1081, 7013, 7100, 7016, 106):
                        dbg('RESP msg(%s)=%s' % (mid, resp[:400] if isinstance(resp, bytes) else str(resp)[:400]))
                    # 可能新增怪物/技能的消息 -> 推送技能袋 (客户端 SQSkill 同步, 防 Dummy)
                    if mid in (6003, 1062, 1179, 1098, 8006):
                        push_skill_bag(conn)
                    # 1404 兑换商城购买 -> 补推道具背包 (客户端 SQProp 1039/1040 扣减同步, v10 §2.4)
                    if mid == 1404:
                        push_prop_bag(conn)
                    # ★★2026-10-02 (审计 §12.1 / §13 排名 8): **真建小红点推送链路**。
                    #   `SuccessEnterGame.lua:38-41` 每次进游戏按序发 105/101/102/104, 而客户端
                    #   这四个号的 recvMessage **都不读应答体** ⇒ "把应答补全"永远点不亮红点。
                    #   亮灯只能靠服务端主动推: 8082 ATTENTIONPUSH(主界面按钮红点) + 1053
                    #   POINT_RECEIVE(子菜单红点)。这里挂在这四个号的应答之后触发。
                    #   四个号各推一次: 推送体很小且**幂等**(NetAttentionPush:updateState 命中
                    #   同 (type,subtype,subid) 是原地刷新; NetPointReveice 是整表替换),
                    #   换来的是"不依赖客户端发送顺序"—— 比只挂其中一个号稳。
                    if mid in (101, 102, 104, 105):
                        log('PUSH reddots after msg(%s): %s' % (mid, _push_reddots(conn)))
            except acct_identity.AccountBindingError as _e:
                log('ACCT 结束 framing-A 连接: %r' % _e)
                identity_failed = True
                # ★2026-10-12: 鉴权失败必须**回一帧明确的错误码再断**，不能只断连接。
                #   只断的话客户端只看到 socket 关闭 → `ReconnectManager` 会无限重连，
                #   玩家看到的是"连接网络中"而不是"账号或密码错误"。
                #   用裸 `[<code>]` 的 JSON（与 1164/1196 处既有错误帧同一形状）——
                #   ⚠ **不要**用 b64json：那条路上客户端按 base64 解，裸 JSON 会被解成空。
                try:
                    _r = pack_login_frame(
                        mid, 0, ('[%d]' % ygzz_auth.SOCKET_BAD_TICKET).encode('utf-8'))
                    if _r:
                        conn.sendall(_r)
                except Exception as _se:
                    dbg('AUTH-DENY send err %r' % (_se,))
                break
            except accounts.AccountsError as _e:
                log('ACCT 妖盟持久化失败 msg(%s): %r' % (mid, _e))
                try:
                    _r = pack_login_frame(mid, 0, b'[0]')
                    if _r:
                        conn.sendall(_r)
                except Exception:
                    pass
                break
            except socket.timeout:
                continue  # keep the login connection alive to catch later frames (1008/...)
            except Exception as _e:
                # ★★2026-10-02 (审计 §9 高severity #2 / §13 排名 7): framing-A 的 **per-request 兜底**。
                #   旧实现这里**只接 socket.timeout** ⇒ 只要 handler 抛一次异常, 控制流就穿出
                #   本循环, 落到下面 "切 LE 路径" 的分支 —— 在一条**登录(framing-A)连接**上
                #   改用 LE 的 read_frame 继续解析, 帧格式完全不同 ⇒ 整条连接错乱,
                #   客户端表现是"卡住/没反应", 而服务端只留一行 'GAME raw err'。
                #   对照: LE 路径一直是有兜底的(:943-958 的 `except Exception` -> 回 [1,{}] 并继续),
                #   这里照抄同一语义: **记日志 + 回既有空成功形状 + 继续服务后续帧**。
                log('FRAMING_A handler err msg(%s) %r' % (mid, _e))
                # ★★2026-10-02 真机事故修复（**日志以 ~6 MB/s 疯涨、2.4 GB 且仍在涨**）：
                #   修前这里**无条件** `continue`。而外层 try 同时包住了"读帧"和"全部 handler 分发"：
                #   对端 RST 之后 `_read_framing_a_frame` 每次都抛 ConnectionResetError ->
                #   被当成"handler 出错" -> 记一行 -> `continue` -> 立刻再读 -> 再抛 …… **死循环**，
                #   且 `mid` 还是**上一帧**的值（实测刷出 **44,000 行/秒** 的
                #   `FRAMING_A handler err msg(5006) ConnectionResetError`，把磁盘当无限缓冲写）。
                #   per-request 兜底的本意是"handler 逻辑抛了就回个空成功形状、继续服务**后续**帧"，
                #   它**不**适用于"这条连接已经不可用"的情况 —— 那种情况必须结束连接。
                #   判据用**异常类型**而不是"哪一行抛的"：连接类异常(socket/OSError)无论是
                #   读帧抛的还是 handler 内部推送抛的(实测就是 `handle_sdk_buy` 推 8084 时撞上
                #   已关闭的 socket)，结论都一样 —— 这条连接废了，重试同一个帧只会空转。
                if isinstance(_e, (ConnectionError, OSError)):
                    log('GAME login conn 不可用(%s) —— 结束该连接(绝不重试同一帧)' % type(_e).__name__)
                    break
                try:
                    _r = pack_login_frame(mid, 0, b'[1,{}]')
                    if _r:
                        # 兜底帧也是这条请求的应答：先声明，否则它会被记成"推送"，
                        # 于是这条请求在账上就成了"未应答"（假红）。
                        WIRE.stage('resp', reason='per-request 兜底 [1,{}]')
                        conn.sendall(_r)
                except Exception:
                    pass
                continue
            finally:
                # ★★2026-10-12 (诊断): 收账。声明了 resp 却一帧没发 -> 记 unanswered。
                #   放在 finally ⇒ 正常/异常/continue/break 四条路径都收账，
                #   不会出现"上一个请求的上下文留到下一个请求"（那会串掉关联号）。
                try:
                    WIRE.end_request()
                except Exception as _de:
                    dbg('WIRE end_request err %r' % (_de,))
                if request_locked:
                    request_lock.release()
    except Exception as e:
        # ★2026-10-12 (排查): 这里原本只有一行 `GAME raw err`。而"连接被外层接走"
        #   意味着**没能走到 per-request 兜底**，这种异常的原始位置光看一行看不出来。
        #   ⇒ 连调用栈一起打进 server-out.log（用 `dbg` 保留原始行不动）。
        log('GAME raw err %r' % e)
        # ★2026-10-12: 这一支是"异常**穿出了 per-request 兜底**"才会到的，
        #   也就是"客户端一个字节都收不到"的那一类。光记一行 repr 查不出位置，
        #   所以把调用栈一起落到 dbg（`logs/server_debug.log`）。
        dbg('GAME raw err traceback:\n%s' % traceback.format_exc())
        try:
            accounts.trace('GAME raw err %r\n%s' % (e, traceback.format_exc()))
        except Exception:
            pass
    finally:
        # ★2026-10-12: 这条 `settimeout` 是为后面的 LE 路径准备的长超时，但它跑在
        #   "socket 可能已被 `_force_close` 关掉"之后 —— 那时它会抛 WinError 10038，
        #   把一条正常的收尾变成 traceback（实测踩到）。吞掉即可：socket 没了，
        #   后面的读取本来就会立刻结束。
        try:
            conn.settimeout(30)
        except Exception:
            pass
    if identity_failed:
        _drop_conn(conn)
        conn.close()
        return
    # ★v32: 切到 LE 路径时, buf 里可能还剩字节(比如 framing-A 帧后面粘着一个 LE 帧)。
    #   旧写法直接换 conn 读 -> 这些字节**静默丢失**。用一个小 shim 先把残留喂给 read_frame,
    #   之后再落到真 socket。这样两条路径之间也不会丢数据。
    try:
        src = _BufShim(conn, buf) if buf else conn
        while True:
            got=read_frame(src)
            if not got: break
            msgid,seqid,obj=got
            conn.reqs = int(getattr(conn, 'reqs', 0)) + 1
            if conn.reqs > MAX_REQS_PER_CONN or int(getattr(conn, 'bytes_in', 0)) > MAX_BYTES_PER_CONN:
                log('GAME LE 连接超限 reqs=%d bytes_in=%d conn=%s -> 断开'
                    % (conn.reqs, int(getattr(conn, 'bytes_in', 0)), _conn_id(conn)))
                DIAG.emit_deny(_conn_id(conn), getattr(conn, 'account_id', None), msgid,
                               None, 'conn_quota', ok=False,
                               err='reqs=%d bytes_in=%d' % (conn.reqs,
                                                            int(getattr(conn, 'bytes_in', 0))))
                break
            # ★★2026-10-12 (诊断): LE 路径与 framing-A 同一套账。
            WIRE.begin_request(conn, msgid, corr=seqid, seq=seqid, body=obj)
            WIRE.stage('resp', reason='LE 分支应答')
            log('GAME recv msgid=%s seqid=%s' % (msgid,seqid))
            dbg('LE msgid=%s obj=%s' % (msgid, json.dumps(obj, ensure_ascii=False)[:120]))
            # ★★2026-10-26（审查 H1）：与 framing-A 同一条纪律 —— 1002 会写档，
            #   必须在门之前用票据绑定身份（LE 路径的 1002 走 GAME_HANDLERS）。
            if msgid == MSG['ROLE_CREATE'] and _auth_active() and not conn.authed:
                _bind_userid_if_real(obj, conn)
            _allowed, _why = _gate_ok(conn, msgid)
            if not _allowed:
                log('AUTH 未认证连接(LE)请求 msg(%s) 被拒 conn=%s' % (msgid, _conn_id(conn)))
                DIAG.emit_deny(_conn_id(conn), None, msgid, [ygzz_auth.SOCKET_BAD_TICKET],
                               _why, code=ygzz_auth.SOCKET_BAD_TICKET, ok=True)
                WIRE.stage('deny', reason='鉴权门拒绝')
                send_frame(conn, msgid, seqid, [ygzz_auth.SOCKET_BAD_TICKET])
                WIRE.end_request()
                continue
            if msgid in (MSG['SDK_LOGIN'], MSG['LOGIN']):
                # 统一为数组契约 [1,[seqid,account,userpwd,macid,hasrole]] (客户端 parseMessage 按数组解析, 对象会失败)
                send_frame(conn, msgid, seqid, [1, ['1', '1', '1', '1', 1]])
            elif msgid == MSG['GAME_SERVER']:
                send_frame(conn, msgid, seqid, [1, [[ADV_HOST, ADV_SOCK_PORT]]])
            elif msgid == MSG['HEARTBEAT']:
                send_frame(conn, msgid, seqid, [1, {}])
            elif msgid == 3002:  # GET_VITALITY (LE 路径): 恢复核准 + roleInfo + 推 1077
                resp = _account_guard_call(handle_get_vitality,
                                           json.dumps(obj, separators=(',', ':')) if isinstance(obj, dict) else None)
                send_frame(conn, msgid, seqid, json.loads(resp.decode('utf-8') if isinstance(resp, bytes) else resp))
                send_frame(conn, 1077, 0, json.loads(vitality_remain_push().decode('utf-8')))
                dbg('LE sent msg(3002) get_vitality + push 1077')
            elif msgid == 1076:  # VITALITY_PUSH_BEGIN (LE 路径): ack + 推 1077
                send_frame(conn, msgid, seqid, [1, []])
                send_frame(conn, 1077, 0, json.loads(vitality_remain_push().decode('utf-8')))
                dbg('LE sent msg(1076) push_begin + push 1077')
            elif msgid == MSG['ROLE_INFO']:
                # ★2026-10-02 (任务 F / D3=C): LE 路径同样要按自报 userid 改绑（见 helper）。
                _bind_userid_if_real(obj, conn)
                send_frame(conn, msgid, seqid, json.loads(role_info_json().encode('utf-8')))
                if accounts.ENABLED:
                    _push_chat_history(conn)
            elif msgid == MSG['ROLE_NAME']:
                send_frame(conn, msgid, seqid, [1, ['测试角色']])
            elif msgid == 103:  # PUSH_LETTER (LE 路径兜底)
                send_frame(conn, msgid, seqid, [1, {}])
                send_frame(conn, 1046, 0, json.loads(LETTER_REV_JSON.decode('utf-8')))
            else:
                _h = GAME_HANDLERS.get(msgid)
                if _h:
                    _fn, _nb = _h
                    try:
                        chat_lock = (accounts.REGISTRY._guild_lock if msgid == 8080 and
                                     accounts.ENABLED and accounts.REGISTRY is not None else nullcontext())
                        with chat_lock:
                            if _nb:
                                resp = _account_guard_call(
                                    _fn, json.dumps(obj, separators=(',', ':')) if isinstance(obj, (dict, list)) else obj)
                            else:
                                resp = _account_guard_call(_fn)
                            body = json.loads(resp.decode('utf-8') if isinstance(resp, bytes) else resp)
                            send_frame(conn, msgid, seqid, body)
                            dbg('LE sent msg(%s) game_handler' % msgid)
                            if msgid == 8080 and isinstance(body, list) and body[:1] == [1]:
                                log('pushed msg(8081) CHAT_TOTAL delta connections=%d' % _push_chat_delta(conn))
                        # 1404 兑换商城购买 -> 补推道具背包 (客户端 SQProp 1039/1040 扣减同步, v10 §2.4)
                        if msgid == 1404:
                            push_prop_bag(conn)
                    except accounts.AccountsError as e:
                        dbg('LE 妖盟持久化失败 msg(%s) %r' % (msgid, e))
                        send_frame(conn, msgid, seqid, [0])
                        raise
                    except Exception as e:
                        dbg('LE game_handler err msg(%s) %r' % (msgid, e))
                        # 2026-09-07: 原 {'code':1} 对象体 -> parseMessage 取空(0元素) -> statecode=0 客户端静默;
                        #   改为服务器既有空成功形状 [1,{}] (statecode=1, msg={}) 并与 logcat 一致可追踪
                        send_frame(conn, msgid, seqid, [1, {}])
                else:
                    dbg('LE no-handler msg(%s) -> [1,{}]' % msgid)
                    send_frame(conn, msgid, seqid, [1, {}])
            # ★★2026-10-12 (诊断): 每个 LE 帧结束都收账（`break` 也会走到这里）。
            try:
                WIRE.end_request()
            except Exception as _de:
                dbg('WIRE(LE) end_request err %r' % (_de,))
    except acct_identity.AccountBindingError as e:
        log('ACCT 结束 LE 连接: %r' % e)
    except socket.timeout:
        log('GAME idle timeout')
    except Exception as e:
        log('GAME err %r' % e)
        dbg('GAME err traceback:\n%s' % traceback.format_exc())
    finally:
        try:
            WIRE.end_request()     # 幂等：任何异常路径都不会把上下文留给下一条连接
        except Exception:
            pass
        _drop_conn(conn)          # v32: 从在线表里摘掉, 否则 /admin 推送会一直往死连接写
        conn.close()

def _run_bound_game(conn, addr, port):
    """先建立身份再分发；失败时清空上下文并关闭，绝不带旧身份继续。"""
    try:
        try:
            accounts.EQ_STATE.unbind()
            if _auth_active():
                # ★★2026-10-26（审查 H1）：鉴权开启时**不按来源 IP 预绑档**。
                #   `bind_peer` 会为陌生 peer 在 `_accounts.json` 发一个新号（**写盘**），
                #   而"来源 IP"本来就不是身份（本机所有 模拟器 客户端是同一个 IP）。
                #   身份一律等 1002/1004 携票绑定 —— 这段预绑正是"未认证 1002 能改档"的入口。
                log('ACCT 鉴权模式：连接不预绑 peer 档，等 1002/1004 携票 conn=%s'
                    % _conn_id(conn))
            else:
                accounts.REGISTRY.bind_peer(accounts.peer_key(addr))
                _set_conn_account(conn, accounts.EQ_STATE.current().id)
        except Exception as e:
            log('ACCT bind 失败 peer=%r: %r' % (addr, e))
            _drop_conn(conn)
            conn.close()
            return
        handle_game(conn, addr, port)
    finally:
        try:
            conn.close()
        finally:
            # ★2026-10-12: 连接结束 -> 清掉**这个账号**的金币基线（见 state.gold_baseline_reset）。
            #   必须在 unbind 之前取，否则拿不到账号 id。
            try:
                _aid = accounts.EQ_STATE.current()
                if _aid is not None:
                    gold_baseline_reset(_aid.id)
            except Exception:
                pass
            accounts.EQ_STATE.unbind()


def start_game_socket():
    # ★v32 在线连接注册表 + 每连接写锁。
    #   为什么要注册表: /admin 后台要给"在线玩家"主动推 1059(邮件列表)/1033(背包),
    #     否则发完邮件玩家必须重开界面才看得到。
    #   为什么要写锁: 业务线程(应答请求) 和 HTTP 后台线程(主动推送) 会**并发写同一个 socket**,
    #     两次 sendall 的字节可能交错 -> 客户端按帧长解析就错位了。所有写都走 _LockedConn.sendall。
    def serve(port):
        s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        # ★★2026-10-12 (压测定位): `listen(16)` 是**并发上不去的第一瓶颈**，而且不在代码里。
        #   实测（`work\_multiuser_load.py`，逐级 1/10/25/30/40 并发）：
        #     25 并发 -> 建连 p95 = 0ms；**30 并发 -> 主建连 p95 = 502ms**（1004 处理仍 14ms）。
        #   即"慢"发生在**三次握手**，不在我们的处理逻辑里 —— `backlog=16` 一旦被填满，
        #   Windows 对超出部分的连接会**延迟/重试**，客户端看到的就是 ~500ms 的建连延迟，
        #   而服务端 CPU/内存几乎不动（实测整机 CPU 0.9%）⇒ 只看资源会误判成"没瓶颈"。
        #   修法：backlog 取 `SOMAXCONN`（Windows 默认 200，够 `MAX_CONNS`）。
        #   ⚠ 这不是"调参凑数"：accept 循环本身很快（只登记 + 起一个线程），
        #     16 个待accept槽位在真机"一批玩家同时上线"时同样会被打满。
        #   ⚠ Windows 上 `socket.SOMAXCONN` == 0x7fffffff（Winsock 的"最大合理值"约定），
        #     直接透传会在日志里印出一个吓人的数字，且没有意义 ⇒ 收敛到
        #     `max(MAX_CONNS, 128)`：够用、可读、可解释。
        try:
            _backlog = max(MAX_CONNS, 128)
            if _backlog > getattr(socket, 'SOMAXCONN', 128):
                _backlog = socket.SOMAXCONN
        except Exception:
            _backlog = 128
        if _backlog > 1024:
            _backlog = 1024
        s.bind((SOCK_HOST,port)); s.listen(_backlog)
        log('GAME socket on %s:%d (backlog=%d)' % (SOCK_HOST,port,_backlog))
        while True:
            c,a=s.accept()
            # ★★2026-10-12 (限额 · 连接数): 超过上限的连接**立刻拒绝并关闭**。
            #   为什么要在这里挡：登录 socket 是长连接，而每连接**一个线程** +
            #   一份 `hashlib`/JSON 工作内存。没有上限时"连接数"这个维度完全不受控
            #   —— 压测脚本或一次误配置就能把进程拖到不可用，而且症状是"所有人一起卡"
            #   而不是"某个玩家出问题"，最难归因。
            #   `MAX_CONNS` 默认 200（单服 50 并发目标下留足余量，见 HANDOFF 的压测一节）。
            with _LIVE_LOCK:
                _n_live = len(LIVE_CONNS)
            if _n_live >= MAX_CONNS:
                log('GAME 连接数超限 %d/%d —— 拒绝 %s' % (_n_live, MAX_CONNS, a))
                DIAG.emit_deny(None, None, None, None, 'conn_limit', ok=False,
                               err='live=%d max=%d' % (_n_live, MAX_CONNS))
                try:
                    c.close()
                except Exception:
                    pass
                continue
            lc = _LockedConn(c, a, port)
            try:
                with _LIVE_LOCK:
                    LIVE_CONNS[c.fileno()] = lc
            except Exception:
                pass
            # ★★2026-10-02 (B 任务 · 多账号): 每个连接线程**先绑账号再干活**。
            #   `copy_context()` 在**父线程**取（此时上下文干净），线程体第一行 bind_peer()
            #   把 peer 写进这个被复制的上下文 ⇒ 线程内所有 handler 的 `EQ_STATE[...]`
            #   自动落到该 peer 的档上，914 处调用点一行都不用改。
            #   这也是全工程**唯一**需要 copy_context 的地方：实测生产代码里没有任何
            #   "在 handler 里新起线程"的点（`_push_mail_delayed` 是死代码，全仓零调用点）。
            #   身份**只取 host 不取 port**：源端口每次重连都变（日志 59230→59231 是同一次登录
            #   内的重连），带上端口等于每次登录都换新档；而且 1007/1005 与 1004 本来就落在
            #   **两条不同连接**上，身份必须能跨连接存活。
            if accounts.ENABLED and accounts.REGISTRY is not None:
                _ctx = contextvars.copy_context()

                threading.Thread(target=_ctx.run, args=(_run_bound_game, lc, a, port), daemon=True).start()
            else:
                threading.Thread(target=handle_game,args=(lc,a,port),daemon=True).start()
    threading.Thread(target=serve,args=(SOCK_PORT,),daemon=True).start()
    # 27002: 与 27001 **完全同构**(同一个 handle_game), 不是"GM 端口"。
    #   历史文档写的"27002（游戏+GM）"与代码不符 —— 真正的 GM 能力在 /admin (18080)。
    threading.Thread(target=serve,args=(SOCK_PORT+1,),daemon=True).start()

# ---- 在线连接管理 (v32) ----
LIVE_CONNS = {}          # fileno -> _LockedConn
_LIVE_LOCK = threading.RLock()

class _LockedConn:
    """给每个连接加写锁 + 供后台线程找到它。见 start_game_socket 的说明。

    ★★2026-10-12 (逐条响应诊断)：`sendall` 同时是**全工程唯一的出站记账点** ——
    所有帧（应答/推送/拒绝）都必须经过它，因此"漏记"在结构上不可能发生。
    记账不吞异常、不改字节：返回值与异常跟改造前逐字节一致。
    """

    def __init__(self, sock, addr, port):
        self._s = sock
        self.addr = addr
        self.port = port
        self.lock = threading.RLock()
        self._live_fd = sock.fileno()  # close() 之后 fileno()=-1，注册清理仍用原值
        # ★2026-10-02 (B 任务): 本连接属于哪个账号（多账号模式由连接线程绑定后写入）。
        self.account_id = None
        self.frame_mode = None
        self.chat_sync_aid = None
        # ★2026-10-12 (多人单服 · 限额与身份)
        self.conn_seq = None        # 诊断用的连接标识（fd 会被复用，必须另有序号）
        self.authed = False         # 是否已通过**票据**鉴权（未鉴权不许绑定/写档）
        self.session_ticket = None  # 本连接绑定的会话票据
        self.userid = None          # 服务端签发的 userid
        self.reqs = 0               # 本连接已处理的请求数（限额用）
        self.bytes_in = 0           # 本连接累计入站字节（限额用）
        self.opened_at = time.time()

    def recv(self, n):
        return self._s.recv(n)

    def sendall(self, data, mid=None, seq=None):
        # 发送仍走原来的锁；失败时**先记账再抛**：我们要的正是"这条帧尝试发了、
        # 失败了"—— 客户端说"没收到"时这就是证据（`sendall` 成功 ≠ 客户端已收到）。
        nbytes = len(data) if isinstance(data, (bytes, bytearray)) else 0
        try:
            with self.lock:
                r = self._s.sendall(data)
        except Exception as e:
            WIRE.note(self, data, False, e, mid=mid, seq=seq)
            raise
        WIRE.note(self, data, True, None, mid=mid, seq=seq)
        with _LIVE_LOCK:
            _BYTES_OUT[0] += nbytes
        return r

    def settimeout(self, t):
        return self._s.settimeout(t)

    def fileno(self):
        return self._s.fileno()

    def close(self):
        with self.lock:
            _drop_conn(self)
            try:
                return self._s.close()
            except Exception:
                # 可能已被 `_force_close` 关掉；"重复关一条连接"不是错误。
                return None

_BYTES_OUT = [0]

def _userid_from_body(body):
    """从 `/ucenter/*` 的 urlencoded 请求体里取 `userid`（实现见 `acct_identity.userid_from_body`）。

    ★ 为什么实现搬到 `acct_identity.py`：`import game_server` 会在**模块级**
      `sys.stdout = _Tee(...)`（本文件 :68）—— 测试里一 import 就会把测试输出静默写进
      **生产日志** `work\\logs\\server-out.log`（本轮真踩到）。抽出去之后，那段逻辑就
      不必 import 服务器也能测。
    """
    return acct_identity.userid_from_body(body)


# ---- 票据：TCP 1004/1002 与 HTTP 刷新共用的唯一取票口径 ---------------------------
# ★★2026-10-26（审查 B1）：票据是**服务端签发**的随机串（`secrets.token_urlsafe(24)`，
#   见 `ygzz_auth.SessionRegistry.issue`），客户端从 HTTP 登录应答的
#   `userData.macid` 里拿到。字段名取 `ticket`（新客户端），兼容 `accessToken`
#   （旧 HTTP 刷新若带 SDK token 的位置，**不是**我们的票 —— 查不到就拒，不会误放行）。
#   ⚠ **不要**把 `macid` 也当票据：1002/1003 的 `macid` 历史上是设备串（客户端送空串），
#     收它等于给"空串=有效票"留一条缝。
_TICKET_FIELDS = ('ticket', 'accessToken')


def _ticket_from_body(body_obj):
    """从请求体（dict / JSON str / bytes）里取票据；取不到回 `''`。"""
    d = body_obj
    if isinstance(d, (bytes, bytearray)):
        try:
            d = json.loads(bytes(d).decode('utf-8', 'replace'))
        except Exception:
            d = None
    elif isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:
            d = None
    if not isinstance(d, dict):
        return ''
    for k in _TICKET_FIELDS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ''


def _bind_userid_if_real(body_obj, conn):
    """按客户端自报的 `userid` 改绑账号（实现见 `acct_identity.bind_userid_if_real`）。

    调用点两处：framing-A 的 1004 分发、LE 路径的 1004 分发。**必须在生成 roleInfo 之前**
    —— roleInfo 读的正是当前账号上下文。
    """
    # 改绑与私有推送共用连接写锁：旧身份的快照不能在改绑后继续发送。
    reg = accounts.REGISTRY if accounts.ENABLED else None
    with (reg._guild_lock if reg is not None else nullcontext()), conn.lock:
        try:
            if LIVE_CONNS.get(conn._live_fd) is not conn:
                raise acct_identity.AccountBindingError('连接已退出在线表，拒绝改绑')
            if _auth_active():
                # ★★2026-10-12: 鉴权开启时，**不**看客户端自报的 userid ——
                #   只认服务端在 HTTP 登录里签发的那张票据（userid 是它的检索键）。
                #   下面这行之前是 `acct_identity.bind_userid_if_real(...)`，
                #   那条路等价于"谁填谁的 userid 就进谁的档"。
                _bind_ticket_if_authed(body_obj, conn)
                return True
            changed = acct_identity.bind_userid_if_real(body_obj, accounts_mod=accounts, log=log)
            if accounts.ENABLED and accounts.REGISTRY is not None:
                acc = accounts.EQ_STATE.current()
                if acc is not None:
                    _set_conn_account(conn, acc.id)
            return changed
        except acct_identity.AccountBindingError:
            accounts.EQ_STATE.unbind()
            _drop_conn(conn)
            raise


def _set_conn_account(conn, aid):
    """同时更新连接归属和在线索引；改绑时摘掉旧身份。"""
    with conn.lock, _LIVE_LOCK:
        if LIVE_CONNS.get(conn._live_fd) is not conn:
            raise acct_identity.AccountBindingError('连接已退出在线表，拒绝登记账号')
        old = conn.account_id
        fd = conn._live_fd
        if old is not None:
            members = accounts.REGISTRY.online.get(old)
            if members is not None:
                members.discard(fd)
                if not members:
                    accounts.REGISTRY.online.pop(old, None)
        conn.account_id = aid
        if old != aid:
            conn.chat_sync_aid = None
        if aid is not None:
            accounts.REGISTRY.online.setdefault(aid, set()).add(fd)


def live_count():
    return len(LIVE_CONNS)

def push_all(msgid, payload):
    """把一帧推给所有在线连接, 返回成功条数。payload 必须是 utf-8 bytes。"""
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    frame = pack_login_frame(msgid, 0, payload)
    n = 0
    for lc in list(LIVE_CONNS.values()):
        try:
            # ★2026-10-11：同 push_account —— LE 连接必须收 LE 帧（见那里的注释）。
            if getattr(lc, 'frame_mode', None) == 'LE':
                WIRE.stage('push', reason='push_all %s' % msgid)
                send_frame(lc, msgid, 0, json.loads(payload.decode('utf-8')))
            else:
                WIRE.stage('push', reason='push_all %s' % msgid)
                lc.sendall(frame)
            n += 1
        except Exception:
            pass
    return n


def push_account(account_id, msgid, payload):
    """★2026-10-02 (B 任务 · 多账号): 只推给**某一个账号**的在线连接。

    为什么必须有它：`push_all` 把同一份 bytes 发给每一个在线连接，而它的 3 个生产调用点
    推的全是**账号私有**内容 ——
      · `/admin/api/push_mails`  1059（"世界邮件表 + 当前账号的已领/已删标记"的混合体）
      · `/admin/api/push_bag`    1033（`prop_bag_json()` 直读 `EQ_STATE['prop']`）
      · `handle_sdk_buy`         8084（嵌了**买家本人**的 roleInfo 与掉落组；**玩家可自行触发**）
    单档时代看不出问题，多账号下就是跨账号数据泄漏。`push_all` 保留给真正的世界级广播。
    """
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    frame = pack_login_frame(msgid, 0, payload)
    filenos = set()
    if accounts.REGISTRY is not None:
        with _LIVE_LOCK:
            filenos = set(accounts.REGISTRY.online.get(account_id) or ())
    n = 0
    for fn in filenos:
        lc = LIVE_CONNS.get(fn)
        if lc is None:
            continue
        try:
            with lc.lock:
                # 取快照后可能已改绑/断开，或 fd 被新连接复用。
                if lc.account_id != account_id or LIVE_CONNS.get(fn) is not lc:
                    continue
                # ★2026-10-11：帧格式必须跟着**这条连接自己的**。本函数原先把 BE 帧发给
                #   一切连接，LE 登录的连接收到会当场花屏（验收工具全走 LE ⇒ 一有推送就崩）。
                #   与 `_send_chat_frame`（8081）同一判据。
                if getattr(lc, 'frame_mode', None) == 'LE':
                    WIRE.stage('push', reason='push_account %s' % msgid)
                    send_frame(lc, msgid, 0, json.loads(payload.decode('utf-8')))
                else:
                    WIRE.stage('push', reason='push_account %s' % msgid)
                    lc.sendall(frame)
                n += 1
        except Exception:
            pass
    return n

def _send_chat_frame(conn, payload):
    """按该连接实际使用的帧格式发送一条 8081 增量。"""
    WIRE.stage('push', reason='8081 聊天增量')
    if getattr(conn, 'frame_mode', None) == 'LE':
        send_frame(conn, 8081, 0, json.loads(payload))
    else:
        conn.sendall(pack_login_frame(8081, 0, payload))

def _push_chat_history(conn):
    """1004 完成身份绑定后给此连接推一次可见历史；同账号重复 1004 不重放。"""
    reg = accounts.REGISTRY if accounts.ENABLED else None
    if reg is None:
        return 0
    with reg._guild_lock:
        aid = current_account_id()
        if not aid:
            raise accounts.AccountsError('聊天历史推送时未绑定账号')
        with conn.lock:
            if conn.account_id != aid:
                raise accounts.AccountsError('聊天历史推送目标与当前账号不一致')
            if conn.chat_sync_aid == aid:
                return 0
            payload = handle_chat_total_msg()
            if not payload.startswith(b'[1,'):
                raise accounts.AccountsError('聊天历史生成失败')
            _send_chat_frame(conn, payload)
            conn.chat_sync_aid = aid
            return 1

def _push_chat_delta(origin):
    """回显发信连接，并把同一条消息推给可见账号的其他在线连接。"""
    payload = chat_push_frame_payload()
    if not payload:
        return 0
    reg = accounts.REGISTRY if accounts.ENABLED else None
    if reg is None:
        _send_chat_frame(origin, payload)
        return 1
    targets = set(chat_push_targets() or ())
    sender = current_account_id()
    if not sender or sender not in targets:
        raise accounts.AccountsError('聊天成功但发信账号不在推送目标中')
    if origin.chat_sync_aid == sender:
        _send_chat_frame(origin, payload)
        n = 1
    else:
        # 极少数客户端若在 1004 前就发 8080，首次全量已含本条，不再叠加增量。
        n = _push_chat_history(origin)
    with _LIVE_LOCK:
        peers = [(lc, lc.account_id) for lc in LIVE_CONNS.values()
                 if lc is not origin and lc.account_id in targets and
                 lc.chat_sync_aid == lc.account_id]
    for lc, aid in peers:
        try:
            with lc.lock:
                if lc.account_id != aid or LIVE_CONNS.get(lc._live_fd) is not lc:
                    continue
                _send_chat_frame(lc, payload)
                n += 1
        except Exception:
            pass                       # 断线连接不影响其他账号与已落盘的聊天记录
    return n


def current_account_id():
    if not accounts.ENABLED:
        return None
    acc = accounts.EQ_STATE.current()
    return acc.id if acc else None

def _drop_conn(conn):
    with conn.lock, _LIVE_LOCK:
        if LIVE_CONNS.get(conn._live_fd) is conn:
            if accounts.REGISTRY is not None:
                _set_conn_account(conn, None)
            LIVE_CONNS.pop(conn._live_fd, None)

def drop_all_conns():
    """★v36: 一键回档后**主动断开所有在线连接**, 返回断掉的条数。

    为什么必须断: 回档把内存里的 EQ_STATE 整体换成了快照内容, 而客户端手里还拿着
    回档前的状态 (背包/怪物/等级)。不断开的话:
      · 客户端下次操作会以"旧世界"的假设发请求, 服务端按"新世界"处理 -> 两边错位;
      · 客户端的本地缓存也不会刷新 (它只在登录/主动拉取时全量重建)。
    断开 -> 客户端自动重连 -> 走一遍完整登录链 -> 状态与磁盘一致。
    """
    n = 0
    for lc in list(LIVE_CONNS.values()):
        try:
            lc.close()
            n += 1
        except Exception:
            pass
    # close() 只清理自己；快照后新接受的连接不能被全量 clear 摘掉。
    return n

# ---------- CDN + HTTP(auth/config) ----------
def b64json(o): return base64.b64encode(json.dumps(o,separators=(',',':')).encode()).decode()
# SQLoginData:setData 会逐字段从 gameData 取配置; 空 gameData 会使这些字段变 nil(导致 notice_url=nil)。
GAME_DATA = {
    'answer': False, 'answer_url': 'https://example.com',
    'bilog': False, 'bilog_url': 'https://example.com',
    'buglog': False, 'buglog_url': 'https://example.com',
    'debugs': False,
    'notice_url': http_base() + '/notice',
    'update': False, 'update_url': http_base() + '/notice',
    'complaints': True, 'complaints_url': 'https://example.com',
    'behavior': False, 'behavior_url': 'https://example.com',
}
SERVER_ITEM = {'serverid':9001,'ip':ADV_HOST,'port':ADV_SOCK_PORT,'name':'Yokai: Spirits Hunt (妖怪正传)','status':2,
               'preopenstate':0,'preopentime':0,'listenbehavior':1,'orderindex':1,
               # ★2026-10-01 (B 任务): 字段名兼容别名 —— 同一份服务器对象在客户端有**两套**读法:
               #   · 活路径 MLoginHome.lua:137-138 `getServerInfoByID(...)` -> `.ip` / `.port`  ✔ 一直是对的
               #   · 遗留路径 NetServerlist.lua:43-44,85-94,122-123 与 LoginServerList.lua:270-271
               #     读 `.netip` / `.netport` -> 旧实现下恒为 nil。
               #   服务端下发的是**裸 dict**, 客户端 (SQLoginData.lua:74-78) 原样 table.insert 存进列表,
               #   所以多给两个同值别名零成本、且让两条读法都能取到真实地址。
               #   (2026-10-01 只读调查发现; 未改字段名, 避免动到已验证的活路径)
               'netip':ADV_HOST,'netport':ADV_SOCK_PORT}
def md5f(p):
    h=hashlib.md5()
    with open(p,'rb') as f:
        for c in iter(lambda:f.read(65536),b''): h.update(c)
    return h.hexdigest()

def _send_cdn_denied(handler, path):
    """★2026-10-02 (审计 §7): CDN 越界请求 —— 回兜底并**留日志**。
    越界尝试本身是安全信号, 必须能在 server-out.log 里回溯(旧实现静默 200,
    把"有人在扫盘"伪装成一次正常的 CDN 未命中)。"""
    log('CDN DENY(越界) path=%r peer=%s' % (path, getattr(handler, 'client_address', None)))
    handler._send(b64json({'code':1}))

# ---- 真机验收脚手架（**默认全关**）------------------------------------------------
# `YGZZ_SEED_USERS="<userid>=<账号>[,<userid>=<账号>...]"`：允许把指定 userid
# **直接**映射到已注册账号并在首次请求时给它签一张会话。
#
# ## 为什么需要它（真机实测的决定性事实）
# 目标客户端 `JUMP_SDK` 对话框的**第一个输入框是 userid 而不是账号名**
# （`LoginHomeSdk.lua:1356-1365 _setLoginUserId()` 把框里的串直接写进
# `SQLoginData.userData.userid`），而它的占位文案是「账号：154079178310301434」
# —— 看上去像账号名，实际是 userid。客户端随后**只带这个 userid** 去
# `/ucenter/serverlist`（真机实测 `body=...&userid=<userid>&serverzone=applestore`）。
# ⇒ 想验"游戏 socket 的票据鉴权"这条真机路径，必须让那个 userid 先有一张票；
#    否则服务端会（**按设计**）回 "会话无效，请重新登录"，而这是**正确行为**。
#
# ## 为什么它不是一个漏洞
# · 默认 `None` ⇒ 一行都不生效；
# · 只在**显式配置**时才把**白名单里的那一个 userid** 映射到**指定账号**；
#   未登记的 userid 仍然 1013；
# · 它不签发"任意账号"的票，也不绕过口令 —— 只是"这台测试机用哪个 userid 进哪个档"。
# ⇒ 生产部署**不要**设这个变量（HANDOFF 里写明）。
def _seed_allowed(peer):
    """真机脚手架（`YGZZ_SEED_USERS`）是否允许对**本次请求**生效。

    ★★2026-10-26（审查 H3）：修前只要设了 `YGZZ_SEED_USERS`，**任何来源**都能拿
      白名单里的 userid 换一张票据（`_deploy_isolated.py` 绑的是 0.0.0.0）。那等于
      "一个环境变量就能穿透鉴权"。现在两条件缺一不可：
        ① `YGZZ_SEED_LOOPBACK=1`（显式声明"我在做本机/模拟器验收"）；
        ② 请求来自回环地址（模拟器 经模拟器 NAT 到宿主时，服务端看到的也是 127.0.0.1）。
      生产部署两个都不该出现；拒绝时只记一行日志，不再签发任何票据。
    """
    if os.environ.get('YGZZ_SEED_LOOPBACK') != '1':
        return False
    host = str(peer or '').rsplit(':', 1)[0].strip().strip('[]')
    return host.startswith('127.') or host in ('::1', 'localhost')


def _seed_users_map():
    raw = os.environ.get('YGZZ_SEED_USERS') or ''
    out = {}
    for part in raw.split(','):
        part = part.strip()
        if not part or '=' not in part:
            continue
        uid, _, acc = part.partition('=')
        uid, acc = uid.strip(), acc.strip()
        if uid and acc:
            out[uid] = acc
    return out


def _seed_issue(userid, store, peer):
    """真机脚手架：把白名单 userid 签到指定账号，返回 `ucenter` 应答或 `None`。

    ★★2026-10-27（真机登录实测）：修前这段只在“带了票但票无效”的分支里，
      而**首次登录**的客户端只带 `userid=`（`ticket=` 为空）⇒ 在到达脚手架之前
      就被 `if not _ticket` 拒成 1013，设备永远拿不到票。现在把“无票 + 回环 +
      显式 `YGZZ_SEED_LOOPBACK=1` + 白名单 userid”也交给它处理。
    安全边界不变：默认不设变量；只对回环来源；只签白名单里的 userid -> 指定账号。
    """
    if not userid:
        return None
    # ★2026-11-06 P 发布：游客登录（YGZZ_GUEST_LOGIN=1）。
    #   发布客户端只弹调试框：账号名（第一个框）+ 邀请码（第三个框），客户端把 userid
    #   编码成 "账号名|邀请码"。这里按账号名建号（不存在才建）并签发会话；
    #   角色名由 `_ensure_role_name` 落成账号名。默认不设变量 = 老行为。
    if (os.environ.get('YGZZ_GUEST_LOGIN') or '').strip() == '1':
        _raw, _, _invite = userid.partition('|')
        _name = ''.join(ch for ch in _raw.strip()
                        if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')[:24]
        if len(_name) < 2:
            raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PARAM,
                                      '账号名需 2-24 位字母/数字/汉字')
        _want = (os.environ.get('YGZZ_INVITE_CODE') or '').strip()
        if _want and _invite.strip() != _want:
            log('AUTH 邀请码不符 name=%r invite=%r peer=%s' % (_name, _invite, peer))
            raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PASSWORD, '邀请码无效')
        if not store.exists(_name):
            _aid = _new_account_for(_name, peer)
            # Guest login does not use this password. Generate a random one instead of
            # deriving it from the account name: a predictable password would let anyone
            # take over the account if password login is enabled later.
            _pwd = 'guest-' + os.urandom(24).hex()
            store.register(_name, _pwd, _aid)
            log('AUTH 游客建号 account=%r aid=%s peer=%s' % (_name, _aid, peer))
        _rec = store.get(_name) or {}
        _aid = _rec.get('aid') or _new_account_for(_name, peer)
        store.bind_userid(userid, _name)
        _ensure_role_name(_aid, _name)
        _old, sess = AUTH['sessions'].issue(userid, _name, _aid, peer=peer)
        log('AUTH 游客登录: userid=%r -> 账号 %r（档 %s）peer=%s'
            % (userid, _name, _aid, peer))
        return {'action': 'guest', 'account': _name, 'aid': _aid,
                'userid': userid, 'ticket': sess.ticket}
    acc_name = _seed_users_map().get(userid)
    if not acc_name or not store.exists(acc_name):
        return None
    if not _seed_allowed(peer):
        log('AUTH 真机脚手架被拒(未显式回环): userid=%s peer=%s' % (userid, peer))
        return None
    _aid = store.get(acc_name).get('aid')
    log('AUTH 真机脚手架: userid=%s -> 账号 %r（档 %s）peer=%s'
        % (userid, acc_name, _aid, peer))
    store.bind_userid(userid, acc_name)
    _ensure_role_name(_aid, acc_name)
    _old, sess = AUTH['sessions'].issue(userid, acc_name, _aid, peer=peer)
    return {'action': 'seed', 'account': sess.account, 'aid': sess.aid,
            'userid': sess.userid, 'ticket': sess.ticket}


# ============================================================================
# ★★2026-10-12 (多人单服 · 登录/注册): `/ucenter/*` 的鉴权主体
#
# 为什么把它放在**模块级函数**而不是 HTTP 方法里：HTTP 方法里只能起服务测，
# 而这段是整套身份判定的判据所在（重复注册/错密码/过期票据/存量认领），
# 必须能在一个不 import game_server 的进程里被逐条验证 —— 所以它的核心逻辑
# 全在 `ygzz_auth` 里，这里只做"编排 + 落库"。
# ============================================================================
def _form_fields(body):
    """把 `/ucenter/*` 的 urlencoded 请求体解成 dict（只做一次解码，键值都 unquote）。"""
    from urllib.parse import unquote_plus
    out = {}
    try:
        for kv in (body or b'').decode('utf-8', 'replace').split('&'):
            if not kv:
                continue
            k, _, v = kv.partition('=')
            if k:
                out[unquote_plus(k)] = unquote_plus(v)
    except Exception:
        pass
    return out


def ucenter_authenticate(body, peer=None):
    """注册 / 登录 → `{action, account, aid, userid, ticket}`。

    失败抛 `ygzz_auth.AuthError`（带 `http_code` 与中文 `msg`）。
    调用方负责把它转成 HTTP 应答；**本函数不碰 HTTP**。

    ## 三条"错了会静默出事"的地方
    1. **刷新服务器列表（客户端只带 userid）**：必须按 userid 找到**仍然有效**的会话。
       找不到就**拒绝**（`1013`）—— 这正是"不能仅凭客户端自报 userid 绑定玩家"的落点。
       若这里回成功，任何人填一个 userid 就能拿到一张有效票据 = 鉴权完全绕过。
    2. **注册的 aid 必须在写口令库之前就位**：两处都要落盘（索引 + 口令库），
       做不到一个事务，所以顺序取"先索引后口令" —— 崩在中间只留下"有账号无口令"，
       下次启动会被 `_seed_auth_from_index` 登记为未认领（可恢复）；
       反过来则会出现"有口令无账号"，玩家输对了密码却拿不到档。
    3. **存量档认领**：`legacy` 且无口令的记录，第一次带口令来就认领它。
       没有这一步，改造前那三个玩家档**永远进不去**；有了它，"无口令登录"不成立。
    """
    f = _form_fields(body)
    account = (f.get('account') or '').strip()
    password = f.get('password') or ''
    userid = (f.get('userid') or '').strip()
    try:
        typ = int(f.get('type') or 0)
    except ValueError:
        typ = 0
    # ★2026-11-07：发布服要求客户端先通过本地声明校验（`YGZZ_NOTICE_OK`）再登录/注册；
    #   `run_server.py` 会设 `YGZZ_REQUIRE_NOTICE=1`。测试/裸 game_server 不设 -> 不拦，
    #   避免把 100+ 个只测协议的老套件全部改一遍。
    if (os.environ.get('YGZZ_REQUIRE_NOTICE') or '') == '1' \
            and (f.get('notice') or '') != '1':
        raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PARAM,
                                  '请先阅读并同意免责/免费声明')
    store = AUTH['store']
    reg = accounts.REGISTRY
    with AUTH['lock']:
        _ticket = ''
        for _k in _TICKET_FIELDS:
            _v = f.get(_k)
            if isinstance(_v, str) and _v.strip():
                _ticket = _v.strip()
                break
        # ---- (A) 刷新服务器列表 / 重连前的再次拉取：**必须携票** ----
        # ★★2026-10-26（审查 B1）：修前这里只凭 `userid` 调 `lookup_userid` 就续签新票，
        #   而 userid 是明文回传的 ⇒ 拿到一个仍有效的 userid 就能无限续期、并进档。
        #   现在只认票据：票查不到（伪造/过期/被顶号）一律 1013。userid 仅用于一致性核对。
        if not account and not password:
            if not userid and not _ticket:
                raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PARAM,
                                          '缺少必要参数（account/password 或 ticket）')
            if not _ticket:
                # ★2026-10-27：首次/无票登录也可能是真机脚手架（显式回环 + 白名单）。
                _seeded = _seed_issue(userid, store, peer)
                if _seeded:
                    return _seeded
                log('AUTH 刷新被拒：只带 userid=%s 没有票据 peer=%s' % (userid, peer))
                raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PASSWORD,
                                          '登录已过期，请重新登录')
            sess, why = AUTH['sessions'].lookup_ticket(_ticket)
            if sess is None:
                # ---- 真机验收脚手架（默认全关；见 `_seed_allowed` 的说明）----
                _seeded = _seed_issue(userid, store, peer)
                if _seeded:
                    return _seeded
                log('AUTH 刷新被拒 ticket=%s… why=%s peer=%s userid=%s'
                    % (_ticket[:8], why, peer, userid))
                raise ygzz_auth.AuthError(
                    ygzz_auth.HTTP_BAD_PASSWORD,
                    '登录已过期，请重新登录' if why == 'expired' else '会话无效，请重新登录')
            if userid and userid != sess.userid:
                log('AUTH 刷新被拒：userid=%s 与票据属主 %s 不符 peer=%s'
                    % (userid, sess.userid, peer))
                raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PASSWORD, '会话无效，请重新登录')
            # ★ 刷新**换一张新票**：旧票在客户端持有期间可能已被复制出去，
            #   而刷新是"我还活着"的证明 —— 换票把窗口收窄到一次登录。
            #   ⚠ `kick=False`：刷新不是"另一台设备登录"，不能把正在游戏的连接踢下线。
            _old, sess = AUTH['sessions'].issue(sess.userid, sess.account, sess.aid,
                                                peer=peer, kick=False)
            return {'action': 'refresh', 'account': sess.account, 'aid': sess.aid,
                    'userid': sess.userid, 'ticket': sess.ticket}

        # ---- (B) 注册 ----
        if typ == 4:                      # UCENTER_PARAM_TYPE.RG_LO
            # P 发布：可选邀请码闸（config.ini 里配了才生效；不配 = 老行为，测试不受影响）
            _want_invite = (os.environ.get('YGZZ_INVITE_CODE') or '').strip()
            if _want_invite:
                _got_invite = (f.get('invite') or '').strip()
                if _got_invite != _want_invite:
                    log('AUTH 邀请码不符 account=%r got=%r peer=%s'
                        % (account, _got_invite, peer))
                    raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PARAM, '邀请码无效')
            ok, why = ygzz_auth.check_account_name(account)
            if not ok:
                raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PARAM, why)
            if store.exists(account):
                log('AUTH 重复注册被拒 account=%r peer=%s' % (account, peer))
                raise ygzz_auth.AuthError(ygzz_auth.HTTP_DUPLICATE, '此账号已被占用')
            ok, why = ygzz_auth.check_password(password)
            if not ok:
                raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PARAM, why)
            aid = _new_account_for(account, peer)
            # ★2026-11-07：注册时生成 6 位重置码（只回这一次；库里只存哈希）。
            _reset_code = ygzz_auth.new_reset_code()
            store.register(account, password, aid, reset_code=_reset_code)
            _out = _issue_for(account, aid, peer, 'register')
            _out['resetcode'] = _reset_code
            return _out

        # ---- (C) 登录 ----
        if not account or not password:
            raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PARAM,
                                      '缺少必要参数（account/password）')
        rec = store.get(account)
        if rec is None:
            raise ygzz_auth.AuthError(ygzz_auth.HTTP_NO_ACCOUNT, '未找到此账号')
        if rec.get('legacy') and not rec.get('hash'):
            # 存量档认领：口令**当场生效**，之后必须凭它登录。
            aid = rec.get('aid') or _new_account_for(account, peer)
            store.claim(account, password, aid)
        else:
            rec = store.login(account, password)
            aid = rec.get('aid') or _new_account_for(account, peer)
        return _issue_for(account, aid, peer, 'login')


def ucenter_reset(body, peer=None):
    """★2026-11-07 自助重置：`account + code + password` -> `{account, resetcode}`。

    · 重置码只在注册/上一次重置的响应里出现过；库内只存 PBKDF2 哈希；
    · 成功后轮换重置码（返回新码），并调用 `Sessions.revoke_account(aid)` 废掉所有旧票；
    · 失败抛 `ygzz_auth.AuthError`，由 HTTP 层转成 `{code, descode}`。
    """
    if not AUTH_ENABLED or AUTH is None:
        raise ygzz_auth.AuthError(ygzz_auth.HTTP_SERVER_CLOSED, '服务器未启用账号系统')
    f = _form_fields(body)
    account = (f.get('account') or '').strip()
    code = (f.get('code') or f.get('resetcode') or '').strip()
    password = f.get('password') or ''
    if not account or not code or not password:
        raise ygzz_auth.AuthError(ygzz_auth.HTTP_BAD_PARAM, '缺少必要参数（account/code/password）')
    with AUTH['lock']:
        rec, new_code = AUTH['store'].reset_with_code(account, code, password)
        try:
            AUTH['sessions'].revoke_account(rec.get('aid'), why='password-reset')
        except Exception as _e:
            log('AUTH 重置后吊销会话失败 account=%r err=%r' % (account, _e))
    log('AUTH 自助重置成功 account=%r peer=%s（重置码已轮换）' % (account, peer))
    return {'account': account, 'resetcode': new_code}


def _new_account_for(account, peer):
    """给一个新账号分配 aid（走 `Registry.login` 的发号器，避免 id 撞车）。

    ⚠ 索引键用 `user:<账号名>`（与 `accounts.bind_user` 同一套前缀）⇒ 与"来源 IP"
      那一类键**永不碰撞**。登录成功后这个映射就是"账号 -> 档"的迁移映射。
    """
    key = accounts._USER_KEY_PREFIX + account
    acc = accounts.REGISTRY.login(key)
    return acc.id


def _ensure_role_name(aid, account):
    """P 发布：账号**首次绑定**时没有 role_name -> 用账号名（@ 前）落一个。

    客户端不再弹建角（1002/1009 保持原样），角色名就来自这里：
    账号 `alice@example.com` -> `alice`；重名追加 2/3…；空名 -> `玩家<aid>`。
    只写本账号自己的档；失败只打日志，不影响登录。
    """
    reg = accounts.REGISTRY
    if reg is None or not aid:
        return None
    try:
        acc = reg.get_or_create(aid)
        if acc is None:
            return None
        cur = str((acc.data or {}).get('role_name') or '').strip()
        if cur:
            return cur
        base = str(account or '').split('@')[0].strip()
        base = ''.join(ch for ch in base
                       if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')[:12]
        if not base:
            base = '玩家' + str(aid)
        used = set()
        for other in reg._known_account_ids():
            try:
                o = reg.get_or_create(other)
                nm = str((o.data or {}).get('role_name') or '').strip()
                if nm:
                    used.add(nm)
            except Exception:
                continue
        name, n = base, 1
        while name in used:
            n += 1
            name = '%s%d' % (base, n)
        acc.data['role_name'] = name
        # ★ 不在这里 `reg.save()`：守住"懒落盘"契约（纯读登录不凭空产生账号档，
        #   `_multiuser_load.py` L5d）。名字先进内存，随下一次正常存档一起落盘；
        #   即使中途重启，下一次登录会按同一规则重算，结果一致。
        log('RELEASE 账号 %r 首次绑定，角色名 -> %r（档 %s）' % (account, name, aid))
        return name
    except Exception as e:
        dbg('RELEASE role_name 兜底失败 %r' % (e,))
        return None


def _issue_for(account, aid, peer, action):
    """建账号上下文 + 签发会话票据。返回给 `_ucenter_login` 的 out dict。"""
    store = AUTH['store']
    reg = accounts.REGISTRY
    # aid 可能来自"口令库"（存量认领）而账号目录里没有该档 —— 那就走 get_or_create
    # 用**种子档**建一份。这是"新号首次建角"的起点：档是种子（未建角），
    # 客户端随后发 1002 建角，由 `handle_role_create` 落角色名。
    acc = reg.get_or_create(aid)
    store.bind_userid(_uid_for_account(account), account)
    _ensure_role_name(aid, account)
    _old, sess = AUTH['sessions'].issue(_uid_for_account(account), account, aid,
                                        peer=peer)
    if acc is not None:
        reg.state.bind(acc)
    log('AUTH %s 成功 account=%r aid=%s peer=%s userid=%s' % (
        action, account, aid, peer, _uid_for_account(account)))
    return {'action': action, 'account': account, 'aid': aid,
            'userid': _uid_for_account(account), 'ticket': sess.ticket}


def _uid_for_account(account):
    """一个账号的**稳定** userid（服务端签发，客户端只是带回来）。

    ⚠ 必须稳定：客户端把它存进 `SQLoginData.userData.userid`，服务端每次登录
      都要能按它找到会话。用随机值就得每次登录都换一个，而"换 userid"在客户端
      看来是换了账号（`config.lua:81 USER_ID` 那种硬编码值尤其明显）。
      做法：首次登录签发一个 18 位随机数，之后固定；并写进口令库的 `users` 表。
    """
    store = AUTH['store']
    d = store._load()
    for uid, name in d['users'].items():
        if name == account:
            return uid
    uid = ygzz_auth.new_userid()
    store.bind_userid(uid, account)
    return uid


# ---- TCP 侧：按**票据**鉴权（★这是"不再仅凭客户端自报 userid 绑定玩家"的落点）----
def _bind_ticket_if_authed(body_obj, conn):
    """玩家连上游戏 socket 后，用**HTTP 登录签发的票据**找回会话并绑定账号。

    调用点：1004（framing-A 与 LE 两条路径）、1002（两条路径的**门之前**）。
    **必须在生成 roleInfo / 写角色名之前**。

    ## ★★2026-10-26 审查 B1：这里以前只按 `userid` 查会话
    旧实现 `sessions.lookup_userid(uid)` 判的是"这个 userid 现在有没有有效会话"，
    而 `userid` 是明文回传、会被缓存的。⇒ 拿到一个仍有效的 userid 的人可以直接连
    游戏 socket 进档，甚至用 `/ucenter/serverlist` 续出新票（票据越续越长）。
    现在改为 `lookup_ticket(ticket)`：票据是 192 位随机串、900 秒过期、同账号登录即作废，
    **不可猜、可撤销**；userid 只用来做一致性核对，不再作为身份凭据。

    ## 三种拒绝（都抛 `AccountBindingError`，调用方回 1013 并结束连接）
    · 没带票 —— 老客户端 / 伪造包；
    · 票查不到（伪造、过期、被顶号、刷新换票后的旧票）；
    · 票与 body 里的 userid 不一致，或与这条连接已绑定的身份不一致（中途换号）。
    **绝不**降级成"发一个新号"—— 那会让玩家看到"角色没了"，并在下次登录时用空档覆盖存档。
    """
    d = body_obj
    if isinstance(d, (str, bytes, bytearray)):
        try:
            d = json.loads(d)
        except Exception:
            d = None
    uid = None
    if isinstance(d, dict):
        uid = d.get('userid')
    uid = str(uid).strip() if uid is not None else ''
    ticket = _ticket_from_body(d)
    if not ticket:
        raise acct_identity.AccountBindingError(
            '1004/1002 未携带票据（仅凭 userid 自报不再能绑定身份）')
    sessions = AUTH['sessions']
    sess, why = sessions.lookup_ticket(ticket)
    if sess is None:
        log('AUTH 游戏连接被拒 ticket=%s… why=%s conn=%s'
            % (ticket[:8], why, _conn_id(conn)))
        raise acct_identity.AccountBindingError('票据%s' % ('已过期' if why == 'expired'
                                                          else '无效/已被顶号'))
    if uid and uid != sess.userid:
        log('AUTH 游戏连接被拒：userid=%s 与票据属主 %s 不符 conn=%s'
            % (uid, sess.userid, _conn_id(conn)))
        raise acct_identity.AccountBindingError('userid 与票据不一致')
    if conn.userid is not None and conn.userid != sess.userid:
        log('AUTH 连接中途换号被拒：已绑 %s，请求 %s conn=%s'
            % (conn.userid, sess.userid, _conn_id(conn)))
        raise acct_identity.AccountBindingError('一条连接不能中途切换账号')
    reg = accounts.REGISTRY
    acc = reg.get_or_create(sess.aid)
    reg.state.bind(acc)
    _set_conn_account(conn, acc.id)
    conn.authed = True
    conn.session_ticket = sess.ticket
    conn.userid = sess.userid
    sessions.mark_used(sess.userid)
    log('AUTH 游戏连接鉴权通过 userid=%s aid=%s account=%r conn=%s'
        % (sess.userid, sess.aid, sess.account, _conn_id(conn)))
    return True


# ============================================================================
class HTTP(BaseHTTPRequestHandler):
    # ★★2026-10-02 (热更通道 (b)): 客户端的下载器 **先发 HEAD 探测再发 GET**。
    #   实测证据（设备 logcat，ver 1.30.001）:
    #       D/Cocos2dxDownloader: onFailure(code:501 ... HttpResponseException:
    #                              Unsupported method ('HEAD') id:1
    #   旧实现只有 do_GET/do_POST/do_DELETE ⇒ `BaseHTTPRequestHandler` 对 HEAD 直接回
    #   **501 Unsupported method ('HEAD')**，而且走的是 `send_error → log_message`，
    #   而 `log_message` 被重写成空 ⇒ **服务端日志里一行都不留**。
    #   后果链: HEAD 失败 -> 下载器判 `ERROR_DOWNLOAD_MANIFEST` -> `AssetsManager:updateError()`
    #   -> 置 JUMP_GAME_LOGO 并在 3 秒后重启 UpdateScene -> **无限重启**。
    #   ⇒ 这就是"客户端从来没成功消费过 /project.manifest"的**最后一环**，而且它在服务端这一侧。
    _head_only = False

    def do_GET(self): self.h()
    def do_POST(self): self.h()
    def do_DELETE(self): self.h()

    def do_HEAD(self):
        """HEAD = 与 GET 同路由、同响应头，但**不写 body**（HTTP/1.1 要求）。"""
        self._head_only = True
        try:
            self.h()
        finally:
            self._head_only = False

    def h(self):
        # ★★2026-10-02 (B 任务 · 多账号): HTTP 侧只有 peer 可用（`self.client_address`）。
        #   这里 bind 是为了 `/admin` —— 它的每个接口都会读写 EQ_STATE（`prop_bag_json()` 之类），
        #   在"未绑定即拒绝落盘"的契约下不 bind 就会踩未绑定。
        #   目标账号由 `/admin?...&acct=u0002` 指定；不指定则用 u0001（第一个签发的账号）。
        #   ⚠ 与 TCP 侧不同：HTTP 线程没有"我是哪个玩家"这回事，所以这只能算**运维视角**的绑定，
        #     真正的玩家身份仍然只从 TCP 连接的 peer 来。
        if accounts.ENABLED and accounts.REGISTRY is not None:
            try:
                # ★2026-10-02 (D5=B): 用 `accounts.parse_acct_param` —— 与 admin_backend 的
                #   强制校验**共用同一个解析器**。修前这里是裸 `split('acct=', 1)`：
                #   一处按严格规则拒、另一处按裸切分取，就是"校验严、绑定松"的分叉，
                #   绕过（`?x=acct=u0001` 之类）正是从那条缝里来的。
                _acct, _aerr = accounts.parse_acct_param(self.path)
                if _aerr:
                    _acct = None            # 非法 -> 不绑定（由 admin_backend 回 400）
                accounts.REGISTRY.bind_admin(_acct or None)
            except Exception as _e:
                print('[ACCT] admin bind 失败: %r' % _e, flush=True)
        # ★★2026-10-12 (限额 · 请求体): HTTP 请求体硬上限。
        #   `Content-Length` 是**客户端声明的**，旧实现照单全收 `self.rfile.read(ln)`
        #   ⇒ 一个 `Content-Length: 4294967295` 的请求就能让服务端一直读到内存耗尽
        #   （而且它不需要真的发那么多字节，只要挂着不发就够占住线程）。
        ln=int(self.headers.get('Content-Length') or 0)
        if ln < 0 or ln > MAX_HTTP_BODY:
            log('HTTP 请求体越界 %d (上限 %d) path=%s peer=%r'
                % (ln, MAX_HTTP_BODY, self.path, getattr(self, 'client_address', None)))
            DIAG.emit_deny(None, None, None, None, 'http_body_limit', ok=False,
                           err='len=%d path=%s' % (ln, self.path))
            self._send_text('请求体过大', 'text/plain; charset=utf-8', code=413)
            return
        body=self.rfile.read(ln) if ln else b''
        p=self.path.split('?')[0]
        # ★v32: 网页后台 (发物品/看状态/推送) 挂在同一个进程的 18080 上。
        #   放这里的理由: 同进程才能直接读改内存里的 EQ_STATE 并 save_state (外部进程改 state.json
        #   会被服务端下一次 save_state 覆盖), 而且能给在线客户端主动推包。
        #   鉴权在 admin_backend 里做 (token + 写操作全量校验)。
        if p == '/admin' or p.startswith('/admin/'):
            if not self.path.startswith('/admin'):
                pass
            try:
                import admin_backend
                if _account_guard_call(admin_backend.handle, self, self.path, body,
                                       self._send_json_admin, self._send_bytes, self._send_text):
                    return
            except Exception as _e:
                log('ADMIN err %r' % _e)
                self._send_text('后台异常: %r' % _e, 'text/plain; charset=utf-8', code=500)
                return
        # ★2026-10-01 (B 任务): 补上 self.client_address —— 这是"客户端到底向哪个 host 请求
        #   服务器列表/登录接口"的唯一硬取证点。旧日志只有 方法+路径+body, 能证明"打到了本服",
        #   但证不了"从哪来"(回环隧道 / 局域网 / 公网), 而这三者导向完全不同的分发方案。
        #   注意: adb reverse 与同机自闭环下这里恒为 127.0.0.1; 局域网/公网玩家会看到真实源 IP。
        try:
            _peer = '%s:%s' % self.client_address[:2]
        except Exception:
            _peer = '?'
        log('HTTP %s %s peer=%s body=%s' % (self.command, self.path, _peer,
                                           ygzz_diag.body_preview(body)[:160]))
        # --- 配置/认证接口 ---
        # ★★2026-10-02 (热更通道 (b)): `/ucenter/vsNumber` 就是客户端 `getEngineURL()` 的落点，
        #   它的响应决定 `AssetsManager:startUpdate` 的 callfunc 走哪一支（AssetsManager.lua:115-155）:
        #       gmHotOpen == 0              -> "关闭热更新" + 弹 NewVersion 面板,
        #                                      confirm 绑到 `cc.Director:getInstance():endToLua()`
        #                                      —— 也就是**点一下就退出游戏**;
        #       gmHotOpen == 1 且 url 非空   -> 大版本更新 (openURL 去下新客户端);
        #       其它                         -> 小版本更新, 这时才真的 `assetsManagerEx:update()`。
        #   旧值恒为 0 ⇒ 客户端**永远走不到**第三条 ⇒ 服务端那个 `/project.manifest` 从来没人消费。
        #   本服要的是第三条，所以**不回** gmHotOpen（不回 = 走 else）。
        #   `islog:0` 是为了**不**踩 `if not netdata.islog then OPEN_LOG = true end`
        #   —— 那行会在服务端不给 islog 时把客户端日志全局打开（Lua 里 0 是真值，所以 0 安全）。
        if p.endswith('/vsNumber'):        resp=b64json({'code':1,'islog':0}); self._send(resp)
        elif p.endswith('/cdnfilesize'):   resp=b64json({'code':1,'size':0}); self._send(resp)
        elif p.split('?', 1)[0].endswith('/ping'):
            # ★2026-11-07：局域网自动发现。裸 JSON（不能 base64），客户端只用它判断
            #   “这个 IP 上是不是本服”，不参与登录鉴权；不回任何账号/票据。
            resp = json.dumps({'ok': 1, 'name': SERVER_ITEM['name'],
                               'ip': ADV_HOST, 'http': ADV_CDN_PORT,
                               'sock': ADV_SOCK_PORT}, ensure_ascii=False)
            self._send(resp)
        elif p.endswith('/reset'):
            self._ucenter_reset(body, p)
        elif p.endswith('/serverlist'):
            self._ucenter_login(body, p)
        elif p.endswith('/hoolailogin') or p.endswith('/aonelogin') \
                or p.endswith('/gametaiwanlogin') or p.endswith('/facebooklogin') \
                or p.endswith('/japanlogin') or p.endswith('/xiao7login'):
            self._ucenter_login(body, p)
        elif p.endswith('/notice') or '/notice' in p:
            # 登录公告(LoginNotice) —— ★必须是【裸 JSON】, 不能 base64
            #   客户端 app/network/HttpManager.lua:51 设了
            #       self.xhr[_event].responseType = cc.XMLHTTPREQUEST_RESPONSE_JSON
            #   所以 xhr.response 已经是"解析好的对象"; LoginNotice.lua:76 又对它做一次
            #       CJson2Table(_event._usedata)
            #   若这里发 base64 字符串(eyJ...), 第二次解析必炸:
            #       [string "app/loginoptimize/LoginNotice.lua"]:76: Expected value but found invalid token at character 1
            #   该错误发生在 HttpManager 的 dispatchEvent 回调里, 会把登录流程打断 -> 卡在标题界面。
            #   (对比: /hoolailogin 等端点的消费方期望 base64, 所以那些仍用 b64json, 不要一起改。)
            self._send_json({'code': 1, 'data': [], 'list': []})
        # --- 资产 CDN: 热更清单 + 资源下发 (通道 (b): 客户端自带 cc.AssetsManagerEx) ---
        #   ★2026-10-02: 旧实现返回 `assets: {}`，version/engineVersion 写死 '1.29.001' —— 那只是占位。
        #   真清单由 work\hotupdate.py 从 ygzz\hotfix_src\ **逐文件算 md5** 生成。
        #   三条不变量(护栏 _hotupdate_manifest_test.py 逐条断言):
        #     ① engineVersion == 目标客户端 assets/src/config.lua:32 的 internal_600
        #        (不等 -> AssetsManagerEx 判成"引擎版本不同" -> 走 UPDATE_ENGINE 去下新客户端)
        #     ② version 来自 hotfix_src\RELEASE.json
        #        (内容变了版本没变 -> 客户端永远认为"已是最新", 改动永远下不去)
        #     ③ packageUrl == http_base() + '/hotfix/' -> 资源真能取到
        #        (取不到 -> 重试 4 次后 updateError() -> 无限重启)
        elif p.startswith(hotupdate.URL_PREFIX):
            _hfp = hotupdate.resolve(p[len(hotupdate.URL_PREFIX):])
            if not _hfp:
                # 越界尝试是安全信号, 必须能在 server-out.log 里回溯
                log('HOTFIX DENY(越界/不存在) path=%r peer=%s' % (p, _peer))
                self._send(b64json({'code': 1}))
                return
            with open(_hfp, 'rb') as _hf:
                self._send_bytes(_hf.read(), hotupdate.content_type(_hfp))
        elif 'project.manifest' in p:
            self._send_json(hotupdate.manifest(http_base()))
        elif 'version.manifest' in p:
            # 旧实现没有这条分支 -> 落到 CDN 兜底返回 base64 的 {'code':1}
            # 客户端 Manifest 解析它会失败 -> ERROR_PARSE_MANIFEST -> updateError -> 无限重启。
            self._send_json(hotupdate.version_manifest(http_base()))
        else:
            # 尝试从 assets 目录服务真实资源(CDN)
            # ★★2026-10-02 (审计 §7 / §13 排名 1, P0 安全): 越界即拒。
            #   旧实现: rel=p.lstrip('/').replace('..',''); fp=os.path.join(ASSETS,rel)
            #   两个判据同时不成立 ——
            #     ① `os.path.join(base, x)` 在 x 是**盘符绝对路径**时**直接丢弃 base**;
            #     ② `replace('..','')` 只挡 '..', 而 `C:/Windows/win.ini` 里根本没有 '..'。
            #   ⇒ 任意文件读。实测(修前)向 18080 发原始 HTTP:
            #        GET /C:/Windows/win.ini                    -> 200 + 92 B
            #        GET /<work>/admin_token.txt      -> 200 + 32 B  (/admin token)
            #        GET /<work>/state.json           -> 200 + 43,855 B (生产存档)
            #   本端点默认绑 0.0.0.0 且无鉴权, 一旦局域网/公网可达即为**远程任意文件读**。
            #   修法: 先 normpath 再要求结果**仍在 root 之内**(用 root+sep 而不是裸前缀,
            #        否则 `...\extracted-evil` 会因前缀匹配而漏过)。
            _root=os.path.normpath(ASSETS)
            _fp=os.path.normpath(os.path.join(_root, p.lstrip('/').replace('..','')))
            if _fp != _root and not _fp.startswith(_root+os.sep):
                _send_cdn_denied(self, p)
                return
            fp=_fp
            if os.path.isfile(fp):
                data=open(fp,'rb').read()
                self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(fp)[0] or 'octet-stream'); self.send_header('Content-Length',str(len(data))); self.end_headers()
                if not self._head_only:
                    self.wfile.write(data)
                return
            self._send(b64json({'code':1}))
    # ---- /ucenter 登录 & 注册（★2026-10-12 多人单服：真实鉴权 + 会话票据）----
    def _ucenter_login(self, body, path):
        """客户端登录/注册的唯一 HTTP 入口。

        ## 客户端契约（逐环有出处，见 `ygzz_auth` 模块顶部那张链路表）
        * `LoginHomeSdk.lua:2125 apRegister()`  注册：`type=RG_LO(4)` + account + password
        * `MLoginHome.lua:218 connectServer()`  登录：`type` = 当前登录方式（`AP_LO=3` 账号密码）
        * `MLoginHome.lua:479 updateServerList()` 刷新：只带 `userid`
        三者都期望 **base64 的 JSON**：`CJson2Table(decodeBase64(event._usedata))`，
        且判据是 `code == 1`（`MLoginHome.lua:294` / `:491`）。失败时 `code ~= 1`，
        客户端会拿 `descode` 直接弹提示（`MLoginHome.lua:493`）。

        ## 未启用鉴权时
        逐字节保持改造前的行为（回显客户端自报的 userid、`code=1`）。
        这是 121 套回归网依赖的路径 —— **不要**在这里"顺手"改默认值。
        """
        if not AUTH_ENABLED or AUTH is None:
            _uid = _userid_from_body(body)
            return self._send(b64json({
                'code': 1, 'accessToken': 'T', 'uid': '1', 'userid': _uid, 'openid': '1',
                'gameData': GAME_DATA,
                'userData': {'userid': _uid, 'username': '测试', 'openid': '1'},
                'serverList': [dict(SERVER_ITEM)]}))
        _t0 = time.time()
        peer = '%s:%s' % self.client_address[:2]
        try:
            out = ucenter_authenticate(body, peer)
        except ygzz_auth.AuthError as e:
            log('AUTH 拒绝 path=%s peer=%s code=%s msg=%s' % (path, peer, e.http_code, e.msg))
            DIAG.emit_deny(None, None, None, None, 'ucenter_auth',
                           code=e.http_code, ok=False, err=e.msg)
            DIAG.emit_http(path, self.command, 200, None, ok=False, err=e.msg,
                           peer=peer, ms=(time.time() - _t0) * 1000.0,
                           note='auth-reject')
            return self._send(b64json({'code': e.http_code, 'descode': e.msg}))
        except Exception as e:
            log('AUTH 异常 path=%s peer=%s %r' % (path, peer, e))
            DIAG.emit_http(path, self.command, 200, None, ok=False, err=repr(e),
                           peer=peer, ms=(time.time() - _t0) * 1000.0)
            return self._send(b64json({'code': ygzz_auth.HTTP_SERVER_CLOSED,
                                       'descode': '服务器错误'}))
        resp = b64json({
            'code': 1, 'accessToken': out['ticket'], 'uid': '1',
            'userid': out['userid'], 'openid': out['aid'],
            # ★2026-11-07：注册成功回一次 6 位重置码；登录/刷新时为空串。
            'resetcode': out.get('resetcode', ''),
            'gameData': GAME_DATA,
            'userData': {'userid': out['userid'], 'username': out['account'],
                         'skdId': out['account'], 'account': out['account'],
                         # ★ 票据放 `macid`：客户端把整个 userData 原样缓存
                         #   （`SQLoginData:setData`），macid 是最"顺理成章"的位置
                         #   （1003 应答里 msg[4] 本来就是它）。
                         'macid': out['ticket'],
                         'openid': out['aid']},
            'serverList': [dict(SERVER_ITEM)]})
        DIAG.emit_http(path, self.command, 200, resp, ok=True, aid=out['aid'],
                       peer=peer, ms=(time.time() - _t0) * 1000.0,
                       note='%s:%s' % (out['action'], out['account']))
        self._send(resp)

    def _ucenter_reset(self, body, path):
        """POST /ucenter/reset：account + code + password -> {code:1, resetcode}。"""
        peer = '%s:%s' % self.client_address[:2]
        try:
            out = ucenter_reset(body, peer)
        except ygzz_auth.AuthError as e:
            log('AUTH 重置被拒 path=%s peer=%s code=%s msg=%s'
                % (path, peer, e.http_code, e.msg))
            return self._send(b64json({'code': e.http_code, 'descode': e.msg}))
        except Exception as e:
            log('AUTH 重置异常 path=%s peer=%s %r' % (path, peer, e))
            return self._send(b64json({'code': ygzz_auth.HTTP_SERVER_CLOSED,
                                       'descode': '服务器错误'}))
        return self._send(b64json({'code': 1, 'descode': '',
                                   'resetcode': out.get('resetcode', '')}))


    def _send(self, s):
        data=s.encode(); self.send_response(200); self.send_header('Content-Type','text/plain'); self.send_header('Content-Length',str(len(data))); self.end_headers()
        if not self._head_only:
            self.wfile.write(data)
    def _send_json(self, o):
        # ⚠ 保持原样 (ensure_ascii 默认 True): /notice 等登录链路接口共用本方法,
        #   不要为了后台好看去改这里 —— 登录公告的解析在客户端 HttpManager/LoginNotice 里。
        data=json.dumps(o).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.end_headers()
        if not self._head_only:
            self.wfile.write(data)
    # ---- 以下三个只给 /admin 后台用 (中文不转义, 便于人看) ----
    def _send_json_admin(self, o, code=200):
        data=json.dumps(o, ensure_ascii=False).encode('utf-8')
        self._send_bytes(data, 'application/json; charset=utf-8', code)
    def _send_bytes(self, b, ctype='application/octet-stream', code=200):
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.end_headers()
        if not self._head_only:
            self.wfile.write(b)
    def _send_text(self, s, ctype='text/plain; charset=utf-8', code=200):
        self._send_bytes(s.encode('utf-8'), ctype, code)
    def log_message(self,*a): pass

# ============================================================================
# ★★2026-10-02 (审计 §11.1 / §13 排名 2, P0): 启动段收进 `__main__` 守卫。
#   修前: 本文件**没有** `if __name__ == '__main__':` (全文 `__main__` 命中 0) ⇒
#     任何 `import game_server` 都会: ① 置 PROTECT_REAL_SAVEFILE=True ② load_state()
#     ③ 重写 work\server.pid ④ 抢 27001/27002 ⑤ ThreadingHTTPServer.serve_forever()
#     抢 18080 并**永久阻塞**。实证哑弹: work\_letter_test.py:9 `import game_server as gs`,
#     它只把 SAVEFILE 指向 _test_state.json, 对'起服务/写 pid'毫无防护
#     (该文件全树无引用, 故从未引爆)。
#   修后: '禁止 import game_server' 从**纪律**变成**结构上不可能** —— import 只拿到
#     常量/函数/类, 零副作用。判据可失败: _b3_address_config_test.py 原先断言
#     '_BOUND 有 3 次 bind', 现已反过来断言 **0 次**; 守卫一旦被移除, 该套件立刻红。
# ============================================================================
if __name__ == '__main__':
    # 启动: 先载入持久化状态, 再起服务器
    # ★★2026-09-12 (v26) 存档写保护授权: 只有本入口 (真正的服务端) 才允许写生产 state.json。
    #   临时脚本/单测忘了设 YGZZ_SAVEFILE 时会在 save_state() 抛错而不是静默覆盖用户存档。
    #   隔离测试服务器 (YGZZ_SAVEFILE=_e2e_state.json) 也走这里, 但它写的不是生产路径, 不受影响。
    import state as _state_mod
    _state_mod.PROTECT_REAL_SAVEFILE = True
    load_state()
    # ★★2026-10-02 (B 任务 · 多账号隔离): `load_state()` 刚把模板档读进 EQ_STATE 的种子账号里，
    #   现在把它交给 accounts 建注册表。**只有真服务端入口**才会走到这里 ——
    #   测试进程不会（它们不跑 __main__），所以 77 套"单档假设"的回归网完全不受影响。
    if accounts.ENABLED:
        accounts.enable(BASE)
        accounts.REGISTRY._load_index()  # 已有账号却无有效索引时拒绝启动，避免重新签发身份
        accounts.register_push(push_account)
        # ★★2026-10-02（妖盟共享实体）：**世界键（`work\accounts\_world.json`）的启动自检**。
        #   必须在这里、且只有这里跑，两个理由：
        #   ① `sociaty.guild_id` 是**弱引用**，名册才是真值 —— 两者漂移（崩溃在两次写盘之间、
        #      或有人直接改账号档）会让 1121 显示"我在甲会"而 1125 按乙会审批，**两边都不报错**；
        #   ② 反过来，若把自检塞进"读一次会数据"的路径（`guild_world.use()`），
        #      "只是读一次榜不改任何档"这条判据会变成间歇性假红（读操作会写盘）。
        #   ⇒ 自检 = 启动一次 + 显式调用（测试里就是 `_guild_world_test.py` 的 G3）。
        import guild_world as _gworld
        try:
            if _gworld.world_check():
                print('[GUILD] 世界键自检：账号缓存已与名册对齐', flush=True)
        except Exception as _ge:
            raise accounts.AccountsError('妖盟世界键自检失败；拒绝启动。') from _ge
        _seed = accounts.SEED or {}
        # 绑定结束：主线程不需要账号上下文（它不处理请求），留着只会掩盖"某处忘了 bind"。
        accounts.EQ_STATE.unbind()
        # ★★2026-10-12 (多人单服): 鉴权库/会话表。
        #   放在这里（`accounts.enable` 之后）不是随意的：`auth_enable()` 要往
        #   `accounts/` 目录写 `_auth.json`，且**要求**多账号已开（否则拒绝启动）。
        try:
            if auth_enable() is not None:
                _s = AUTH['store']
                print('[AUTH] 已启用: 账号 %d 个（未认领 %d）/ 票据 TTL %.0fs'
                      % (_s.registered_count(),
                         sum(1 for r in _s.accounts().values() if not r.get('hash')),
                         AUTH['sessions'].ttl), flush=True)
        except Exception as _ae:
            print('[AUTH] ★启用失败，拒绝启动: %r' % (_ae,), flush=True)
            raise
        # ★★2026-10-26（审查 H3/P0-3）：**对外配置的硬闸**。
        #   `YGZZ_PUBLIC=1` 是"这台服要让别人连"的显式声明；它必须同时满足
        #   鉴权 + 多账号，否则就是一个"任何人填 userid 就进档"的公开服。
        #   默认不开 —— 回归网/单人档仍按老口径跑（AUTH_ENABLED 默认关）。
        if os.environ.get('YGZZ_PUBLIC') == '1':
            if not _auth_active():
                raise SystemExit(
                    '[AUTH] ★YGZZ_PUBLIC=1 但鉴权未启用（需要 YGZZ_AUTH=1 + '
                    'YGZZ_MULTI_ACCOUNT=1）—— 拒绝以对外多人服配置启动。')
            if not accounts.ENABLED:
                raise SystemExit('[AUTH] ★YGZZ_PUBLIC=1 但多账号未启用 —— 拒绝启动。')
            print('[AUTH] 对外配置自检通过: 鉴权=开 / 多账号=开 / 监听=%s / 下发=%s'
                  % (BIND_HOST, ADV_HOST), flush=True)
        elif not _auth_active() and BIND_HOST not in ('127.0.0.1', 'localhost', '::1'):
            print('[AUTH] ⚠ 未开鉴权却监听 %s —— 仅限单人/熟人测试；'
                  '对外请设 YGZZ_AUTH=1 + YGZZ_MULTI_ACCOUNT=1 + YGZZ_PUBLIC=1'
                  % BIND_HOST, flush=True)
        print('[DIAG] 逐条响应诊断: dir=%s enabled=%s' % (DIAG.dir, DIAG.enabled), flush=True)
        # 只打印"能一眼看出模板对不对"的两个字段 —— 上多账号最怕的就是模板档是空的，
        # 那会让**所有新账号**都是空档（老玩家也会在下次 save 时被覆盖）。
        print('[ACCT] 多账号已启用: root=%s 模板档 %d 键 gold=%s player_lv=%s'
              % (accounts.REGISTRY.root, len(_seed),
                 _seed.get('gold'), _seed.get('player_lv')), flush=True)
    else:
        print('[ACCT] 多账号未启用（设 YGZZ_MULTI_ACCOUNT=1 打开）', flush=True)
    # ★v32: 写 pid 文件 —— 让一键启动脚本能**精确**杀掉本服务端, 而不必用
    #   `Stop-Process -Name python` (那会把本机所有 python 全杀掉) 或靠命令行模糊匹配。
    #   脚本 (ygzz_up.ps1) 仍会做"端口属主 == 新 PID"的二次校验, pid 文件只是第一优先线索。
    _PIDFILE = os.path.join(BASE, 'server.pid')
    try:
        with open(_PIDFILE, 'w', encoding='ascii') as _f:
            _f.write(str(os.getpid()))
    except Exception as _e:
        print('[PIDFILE] 写入失败: %r' % _e, flush=True)
    print('[SAVEFILE] %s  (real-save write %s)' % (
        SAVEFILE, 'ALLOWED (this is the server entry)' if _state_mod._is_real_save() else 'N/A (isolated)'), flush=True)
    if _state_mod._is_real_save():
        # ★v36: 这份快照是"回到今天开服前"的锚点, 必须带说明 (旧实现没 note, 后台列表里看着像野快照)。
        _state_mod.backup_state('server-start', note='服务端启动时自动留底')
    log('SAVEFILE=%s' % SAVEFILE)
    dbg('SAVEFILE=%s' % SAVEFILE)
    dbg('EQ_STATE gold=%s next_monster_ucode=%s' % (EQ_STATE.get('gold'), EQ_STATE.get('next_monster_ucode')))
    log('EQ_STATE gold=%s draw=%s next_monster_ucode=%s' % (
        EQ_STATE.get('gold'), EQ_STATE.get('draw'), EQ_STATE.get('next_monster_ucode')))
    threading.Thread(target=start_game_socket,daemon=True).start()

    # ★v32: 把后台需要的引用**注入** admin_backend。
    #   为什么不让它 import game_server: 坑点 #75 —— 那会再启动一个服务器。
    import admin_backend
    admin_backend.configure(push_all=push_all, push_account=push_account,
                            live_count=live_count,
                            drop_all=drop_all_conns,
                            started_at=time.time(), base_dir=BASE)
    _admin_token = admin_backend.get_token()
    print('[ADMIN] 后台地址: http://127.0.0.1:%d/admin?t=%s' % (CDN_PORT, _admin_token), flush=True)
    print('[ADMIN] token 文件: %s' % admin_backend._TOKEN_FILE, flush=True)
    dbg('ADMIN ready on port %d' % CDN_PORT)

    log('Starting CDN+HTTP on %s:%d' % (CDN_HOST,CDN_PORT))
    # ★2026-10-01 (B 任务) 地址自述横幅: 多机部署最容易踩的坑是"bind 对了但下发地址没改"
    #   (或反之) —— 客户端连到无人监听的地址, 表现为卡登录/一直重试, 而服务端日志一切正常。
    #   把两组地址并排打出来, 一眼就能看出是否配套。
    print('[ADDR] bind: game=%s:%d game2=%s:%d http=%s:%d' % (
        SOCK_HOST, SOCK_PORT, SOCK_HOST, SOCK_PORT + 1, CDN_HOST, CDN_PORT), flush=True)
    print('[ADDR] advertise(下发给客户端): game=%s http=%s  %s' % (
        adv_base(), http_base(),
        '(默认回环 = 同机/adb reverse 模式)'
        if ADV_HOST == '127.0.0.1' else '(非回环 = 需客户端能路由到该地址)'), flush=True)
    if ADV_HOST != '127.0.0.1' and ADV_HOST not in ('0.0.0.0',):
        print('[ADDR] 提示: 非回环下发地址, 请确认 (a) 防火墙放行 %d/%d/%d (b) 客户端侧能解析/路由到 %s'
              % (SOCK_PORT, SOCK_PORT + 1, CDN_PORT, ADV_HOST), flush=True)
    ThreadingHTTPServer((CDN_HOST,CDN_PORT),HTTP).serve_forever()
