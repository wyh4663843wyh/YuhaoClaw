#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书 · 采集器 → 话术库自学习 桥接
================================================================
问题: 采集器(collect_inbox/collect_comments)已经抓到【真实私信/评论】,
     落在 out/leads_raw.csv / leads_comments.csv / leads_classified.csv。
     但话术库自学习闭环(reply_self_learn)没有接住它们——样本躺在硬盘上没被吃。

本脚本把"采集"与"自学习"打通:
  1. 从 csv 自动抽取【有信息量】的留言/评论为样本(过滤 广告/空/纯联系方式/无意义短句)
  2. 每个样本带出 来源note + source_hint(赛道)
  3. 调 learn_from_samples → 三路择优 → 打分 → 星级
  4. 星级合格的自动回灌到 reply_self_generated.yaml
  → 话术库每跑一次采集就自动长一次肉, 零人工。

用法(纯函数/CLI):
  python collect_learn_samples.py            # 读全部3个csv, 抽样本, 回灌
  python collect_learn_samples.py --dryrun   # 只抽样本+演示择优, 不写叠加层
  python collect_learn_samples.py --limit 20 # 只取前20条
"""
from __future__ import annotations

import os, sys, csv, re, argparse, glob

HERE = os.path.dirname(os.path.abspath(__file__))
INTER = os.path.dirname(HERE)          # interceptor/
OUT = os.path.join(INTER, "out")
sys.path.insert(0, HERE)

# ★ 升级2(自动人设强化) · 分区感知: 自学习闭环应读【当前 profile】的采集产物,
#   而非写死全局 out/。默认空 profile → 回退全局 out/(向后兼容)。
#   collect_inbox/collect_comments 落盘到 out/@<profile>/, 这里需同步读对分区。
import json as _json
_SESSION_FP = os.path.join(INTER, "console", "session_ctx.json")   # console 写当前 profile → 闭环读

def _current_profile():
    """读取 console 会话上下文里的当前 profile(增强1已设置); 空则回退全局。"""
    try:
        with open(_SESSION_FP, encoding="utf-8") as f:
            ctx = _json.load(f) or {}
        return (ctx.get("profile") or "").strip()
    except Exception:
        return ""


def _resolve_profile(cli_profile=""):
    """--profile 优先; 否则读 console 会话上下文; 兜底空(全局 out/)。"""
    if cli_profile:
        return cli_profile
    return _current_profile()

def _profile_out(profile=""):
    profile = profile if profile is not None else ""
    if not profile:
        return OUT
    safe = profile.strip().replace("..", "_").replace("/", "_").replace("\\", "_") or "default"
    return os.path.join(OUT, "@" + safe)

# 采集器产物(按当前 profile 解析)
def _csv_files(profile=""):
    out_dir = _profile_out(_resolve_profile(profile))
    return {
        "raw": os.path.join(out_dir, "leads_raw.csv"),
        "classified": os.path.join(out_dir, "leads_classified.csv"),
        "comments": os.path.join(out_dir, "leads_comments.csv"),
    }

# ---- 样本类型判定 ----
# ① 导流技巧样本(最值钱): 含蓄/谐音/间接留联系方式。平台对导流监控严(发多了被屏蔽),
#    "怎么不违规地把联系方式递出去/接过来"本身就是高价值话术, 必须专门采集学习, 不能当垃圾滤掉。
#    命中 → 归类为 lead_angle 类型(高优先)。
# ⚠️ 手机号谐音增强(2026-09-06): 平台防导流, 用户常拆散11位号码规避检测,
#    如 1-8-2-0-1-3-9 / 1 3 5 8 0 0 1 3 0 0 0 / 1·3·5·8·0·0·1·3·0·0·0 等。
#    用方案A: 以1开头, 数字与单个非数字交替, 总数字≥7 → 识别 9位/11位 打散号, 不误伤日期/序数。
#    (之前用"完整11位+分隔"的过严正则, 漏检 9位打散如 1-8-2-0-1-3-9; 方案A 更平衡)
_DIGIT_SCRAMBLED = r"1(?:\D?\d){6,}\D?"
_CONTACT_PAT = re.compile(
    r"(\d{5,}|1[3-9]\d{9}|[Ww][Xx]|[Qq]{2}|扣扣|qq|二维码|扫我|扫码|加v|加微|加薇|加ㄨ|"
    r"薇[信心]|微[信心]|威信|绿泡泡|VX|vx|Vx|＋v|\+\s?v|1[3-9][- .]?\d{2,4}[- .]?\d{4}|"
    r"lian|联[系合]|联系我|找我|私我|私我聊|V我|＋我|[+\+➕＋]\s?(v|V|v信)|"
    r"加个|加一|留一下|留个|发我|给你发|我给你|我发你|后台|主页|简介|置顶|看主页|看简介|"
    r"[➕＋]|威信|📮|✉|Telegram|电报|(?<![a-z])tg(?![a-z])|(?<![a-z])ins(?![a-z])|(?<![a-z])ig(?![a-z])|" + _DIGIT_SCRAMBLED + r")", re.I)
# ② 无信息量/纯情绪词(不够当学习样本, 该滤)
_TRIVIAL = re.compile(
    r"^(嗯|好|可以|行|对|是的|谢谢|ok|呵呵|哈哈|666|好的|那|是吧|哦|这样啊|嗯嗯|收到|在吗|你好|置顶)", re.I)
# ③ 广告/推销/引流垃圾(真垃圾, 该滤: 不是技巧而是外挂/互粉/白嫖党/无关推销)
_AD = re.compile(r"(代发|互粉|互赞|点赞互|涨粉|招代理|加盟|办理证书|免费领|点击链接|薇商|秒杀|薅羊毛|白嫖|刷|回收|咖啡|茶叶|保健品|祛斑|减肥|代购|批发|供货|机器|设备|加盟费|注册|法人|代办)")
# ★ 业务相关度门槛: 评论采集会混入非目标笔记的评论(美食/探店等), 需命中目标赛道词
_BIZ_WORDS = re.compile(
    r"(研学|带团|导游|地接|文旅|AI|OPC|接单|接活|任务|赚钱|副业|课程|培训|"
    r"考证|认证|AIGC|agent|智能体|变现|讲师|签约|素材|干货|失眠|焦虑|贴钱|赔钱|"
    r"内耗|导服|薪资|收入|线路|讲解|获客|选题|文案|建站|单价|一单|多少钱|报名|"
    r"初阶|进阶|班|大模型|提示词|外包|远程|兼职|自由职业|宝妈|失业|"
    r"AIGC|制作经验|技能|想学|转行|入行|入门|教程|怎么做|咋做|如何|"
    r"发你|给你|我发|加|联系|微信|后台|主页|简介|置顶|留个|私我)")


# ---- 样本类型分类 ----
def classify_sample(msg):
    """返回样本类型: 'lead_angle'(导流技巧, 高优先) / 'normal'(常规咨询) / None(该滤的垃圾)。

    ⚠️ 优先级铁律(用户2026-09-06权威意见):
    导流/联系方式(lead_angle)命中 → 直接返回, 不再被业务相关词门槛(_BIZ_WORDS)拦截。
    因为"含蓄/不违规地递联系方式"本身就是高价值话术, 哪怕只有"加v"两字也要采集。
    → 先判 广告垃圾/纯附和词(该滤), 再判 导流(必采集), 最后才判 业务相关度(常规咨询).
    """
    m = (msg or "").strip()
    if len(m) < 2:                 # 导流短句如"加v"只有2字, 不能因过短丢掉
        return None
    if _AD.search(m):              # 广告垃圾(代发/互粉/白嫖/薅羊毛) → 滤
        return None
    if _TRIVIAL.match(m):         # 纯附和词(嗯/好/谢谢/666) → 滤
        return None
    # 联系方式/导流技巧 → 高优先采集(不是垃圾! 哪怕只有"加v/薇信"也要学)
    if _CONTACT_PAT.search(m):
        return "lead_angle"
    if len(m) < 3:                # 非导流但太短(如"就这") → 无信息量, 滤
        return None
    # 无业务相关度(美食/探店) → 滤
    if not _BIZ_WORDS.search(m):
        return None
    return "normal"

# 赛道判定(source_hint): 从来源笔记/来源类型匹配
def _source_hint(text, src_type="", note=""):
    t = text + " " + (note or "") + " " + (src_type or "")
    # ⬇︎ 示例关键词: 请按你实际赛道/人群改写。纯示例, 用于 demo 与自测。
    if any(w in t for w in ("OPC", "接单", "任务", "接活", "赚钱", "副业")):
        return "opc_recruit"
    if any(w in t for w in ("研学", "带团", "导游", "地接", "文旅", "向导", "研学导师")):
        return "xueyan_ai"
    if any(w in t for w in ("培训", "课程", "考证", "认证", "技能", "学AI", "职业")):
        return "ai_training"
    return ""


def _is_feedable(msg):
    """判断这条留言/评论是否值得作为学习样本(必须有业务相关度)。

    ⚠️ 2026-09-06 修正(用户权威意见):
    导流/联系方式(lead_angle)——比如 lian起来/我➕你稍等/加v/薇信/看主页——不是垃圾!
    小红书平台对导流监控严(发多了被屏蔽),"怎么含蓄/不违规地把联系方式递出去接过来"
    本身就是最值钱的高价值话术, 必须专门采集学习。所以 _CONTACT_PAT 命中 → 照样可学。
    这里只滤: 广告垃圾(代发/互粉/白嫖) / 纯附和词(嗯/好/谢谢) / 无业务相关度(美食探店)。
    """
    return classify_sample(msg) is not None


def _sample_type(msg):
    """返回样本类型(用于后续按类型归档): 'lead_angle' / 'normal' / None。"""
    return classify_sample(msg)


def _read_csv(fp):
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def extract_samples(limit=0, profile=""):
    """从采集器产物抽取样本, 返回 [{msg, note, src_type, source_hint}, ...]。"""
    samples, seen = [], set()
    _csv = _csv_files(_resolve_profile(profile))

    # ① 私信原始(leads_raw): recent_text = 对方最新留言
    for r in _read_csv(_csv["raw"]):
        msg = (r.get("recent_text") or "").strip()
        note = (r.get("note_thread") or r.get("note") or "").strip()
        st = (r.get("src_type") or "").strip()
        stype = classify_sample(msg)
        if stype is None or not _is_feedable(msg):
            continue
        key = msg[:30]
        if key in seen:
            continue
        seen.add(key)
        samples.append({"msg": msg, "note": note, "src_type": st,
                        "sample_type": stype,
                        "source_hint": _source_hint(msg, st, note)})

    # ② 私信分级(leads_classified): 留言
    for r in _read_csv(_csv["classified"]):
        msg = (r.get("留言") or "").strip()
        note = (r.get("来源笔记") or "").strip()
        st = (r.get("来源类型") or "").strip()
        stype = classify_sample(msg)
        if stype is None or not _is_feedable(msg):
            continue
        key = msg[:30]
        if key in seen:
            continue
        seen.add(key)
        samples.append({"msg": msg, "note": note, "src_type": st,
                        "sample_type": stype,
                        "source_hint": _source_hint(msg, st, note)})

    # ③ 评论(leads_comments): body 为评论文本
    for r in _read_csv(_csv["comments"]):
        msg = (r.get("body") or "").strip()
        note = (r.get("note") or "").strip()
        st = (r.get("src_type") or "").strip()
        # 评论里"回复了你的评论"的子行 action 可能是空, 只取 real 评论
        stype = classify_sample(msg)
        if stype is None or not _is_feedable(msg):
            continue
        key = msg[:30]
        if key in seen:
            continue
        seen.add(key)
        samples.append({"msg": msg, "note": note, "src_type": st,
                        "sample_type": stype,
                        "source_hint": _source_hint(msg, st, note)})

    if limit and limit > 0:
        samples = samples[:limit]
    return samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dryrun", action="store_true", help="只抽取+演示择优, 不写叠加层")
    ap.add_argument("--no-llm", action="store_true", help="不调LLM, 只走stdlib+scene(秒到, 适合先浏览样本)")
    ap.add_argument("--limit", type=int, default=0, help="只取前N条")
    ap.add_argument("--profile", default="", help="指定账号分区(如 YOUR_DEVICE_SERIAL_1); 空=读console会话或默认out/")
    args = ap.parse_args()

    samples = extract_samples(args.limit, args.profile)
    # 若 --no-llm, 直接关掉全局 LLM 开关(不写叠加层, 只影响本次进程)
    if args.no_llm:
        import reply_self_learn as RL_meta
        RL_meta.DEFAULTS["llm_enabled"] = False

    print(f"从采集器产物抽取到 {len(samples)} 条可学样本 "
          f"(其中导流技巧 {sum(1 for s in samples if s.get('sample_type')=='lead_angle')} 条):")
    for s in samples[:20]:
        sh = f"[{s['source_hint']}]" if s["source_hint"] else ""
        tag = "【导流】" if s.get("sample_type") == "lead_angle" else ""
        print(f"   - {s['msg'][:38]} {sh} {tag}")

    if not samples:
        print("无样本可学(或全被过滤)。请先跑采集器 collect_inbox / collect_comments。")
        return

    # 调闭环
    import reply_self_learn as RL
    write = not args.dryrun
    res = RL.learn_from_samples(samples, write=write)
    print(f"\n{'='*50}\n闭环结果: {len(res)} 条样本完成诊断/择优")
    fed = sum(1 for e in res if e["best"]["stars"] >= RL._load_config().get("min_stars_to_feed", 2))
    print(f"其中 ★★及以上: {fed} 条")
    # 展示脱敏摘要
    from collections import Counter
    scene_cnt = Counter(e["key"] for e in res)
    print("\n命中场景分布:")
    for k, c in sorted(scene_cnt.items(), key=lambda x: -x[1]):
        print(f"   {k}: {c}")
    if write:
        meta, _ = RL._load_meta()
        print(f"\n>>> 已回灌叠加层 {os.path.basename(RL.OVERLAY_FP)}, 话术库长肉完成")


if __name__ == "__main__":
    main()
