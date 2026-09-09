#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
升级3 · 数据漏斗 / 升级4 · 风控加固 / 升级5 · 合规审计
--------------------------------------------------
纯函数集，供 server.py 路由调用，无状态（输入数据 → 输出统计/判定）。
- funnel_note(note_rows)      : 按来源笔记聚合 引流→回复→真商机 漏斗
- compliance_audit(text, ch)  : 红线审计，判违规等级 + 给合规改写建议
- wind_control(text, act)     : 敏感操作风控（二次确认标记 / 频控上限）
"""
from __future__ import annotations
import re


# =====================================================================
# 升级4 · 风控加固 —— 红线词库(对齐《社区规范》, 公开区最严/私信放宽)
# =====================================================================
# ★ 公开区(评论区/笔记正文) 最严: 不报价/不导流/不卖课/不绝对化/不第三方名
PUBLIC_RED = [
    # —— 联系方式(明文直接违规) ——
    "微信", "wx", "vx", "VX", "薇", "薇信", "威信", "绿泡泡", "企鹅",
    "添加", "加V", "加v", "+v", "＋v", "➕", "号码", "手机号", "手机",
    "电话", "q号", "qq", "扣扣", "二维码", "扫码", "扫我",
    # —— 公开直引(评论区"私信我/加我"优先监控) ——
    "私信我", "私聊我", "咨询我", "找我聊", "私我", "主页",
    # —— 广告/卖课/报价 ——
    "报名", "课程", "课程价", "报名费", "4999", "4980", "20-60", "元/条",
    "包接单", "包涨薪", "包升学", "保证", "稳赚", "躺赚", "绝对", "最便宜",
    # —— 绝对化承诺(营销极限词) ——
    "全网最", "行业第一", "全国第一", "没有之一", "yyds", "无敌", "百分百",
]
# ★ 私信: 可报课程价(明示授权), 但禁联系方式/谐音/旧价/包接单/任务单价
PRIVATE_RED = [
    "微信", "wx", "vx", "VX", "薇", "薇信", "威信", "绿泡泡", "+v", "➕",
    "手机号", "电话", "二维码", "扫码", "扫码加",
    "4980", "20-60", "元/条", "包接单", "包涨薪", "包升学",
    "保证接单", "保证赚", "稳赚", "躺赚", "百分百", "全网最",
]

# 第三方平台/软件名(展示即风险, 公开内容一律泛称化)
THIRD_PARTY = ["workbuddy", "豆包", "chatgpt", "kimi", "deepseek", "claude",
               "文心一言", "通义", "midjourney", "即梦", "剪映", "notion", "飞书"]

# 敏感操作(需二次确认 / 风控标记)
SENSITIVE_ACTS = {
    "send_comment":   {"label": "公开发评论", "red": True, "need_confirm": True},
    "send_dm":        {"label": "发私信", "red": True, "need_confirm": True},
    "auto_reply":     {"label": "白名单自动回复", "red": False, "need_confirm": True},
    "add_friend":     {"label": "添加好友/加微", "red": True, "need_confirm": True, "blocked": True},
}


def _find(red_list, text):
    """返回命中的红线词(去重)。"""
    t = (text or "").lower()
    return [w for w in red_list if w in t]


def compliance_audit(text, channel="public"):
    """升级5: 红线审计。channel=public|private。返回 {ok, level, hits, reason, rewrite_ok}。
    level: pass(通过) / red(命中, 需改) / block(绝对化, 不建议发)。"""
    t = text or ""
    if channel == "private":
        hits = _find(PRIVATE_RED, t)
    else:
        hits = _find(PUBLIC_RED, t)
    # 第三方名(公开/私信都尽量规避, 公开内容必须泛称)
    tp = [w for w in THIRD_PARTY if w in t.lower()]
    all_hits = hits + tp
    if all_hits:
        level = "red"
        reason = "命中禁用词: " + ", ".join(all_hits[:8])
        if "绝对化" in str(all_hits) or any(w in ("行业第一", "全国第一", "百分百", "全网最") for w in all_hits):
            level = "block"
        # 合规改写建议(把命中的明显词替换为泛称/删掉)
        rewrite = t
        for w in tp:
            rewrite = rewrite.replace(w, "AI 工具")
        # 联系方式/谐音 → 提示删掉并改走合规承接路径(关注/置顶/私信真人)
        if hits:
            rewrite += "【整改提示】公开区不留联系方式/报价; 引导走 '关注+私信真人' 的合规路径。"
        return {"ok": False, "level": level, "hits": all_hits, "reason": reason,
                "rewrite_ok": True, "rewrite": rewrite}
    return {"ok": True, "level": "pass", "hits": [], "reason": "", "rewrite_ok": False}


def wind_control(act, hit_count=0, audit=None):
    """升级4: 敏感操作风控。返回 {need_confirm, blocked, reason}。"""
    meta = SENSITIVE_ACTS.get(act, {"label": act, "need_confirm": False, "red": False})
    out = {"act": act, "label": meta.get("label", act), "need_confirm": meta.get("need_confirm", False),
           "blocked": meta.get("blocked", False), "reason": ""}
    if meta.get("blocked"):
        out["reason"] = "平台严禁的违规动作(加微/导流), 禁止机器执行, 仅真人在线下/私信合规承接。"
    if audit and not audit.get("ok"):
        out["need_confirm"] = True
        out["reason"] = audit.get("reason", "")
    return out


# =====================================================================
# 升级3 · 数据漏斗 —— 按来源笔记聚合
# =====================================================================
# 口径:
#   note     来源笔记名(引流入口)
#   reach    触达(该笔记引流来的私信条数, 一条用户算一次)   = 潜在
#   reply    真人已回(回流表"已回?" = 已回/已自动回)
#   biz      真商机(回流表"真商机?" 为宜/像)
#   rate_r   回复率 = reply/reach
#   rate_b   商机率 = biz/reach
#   score    综合分(商机优先 + 回复率, 用于选题回顾排序)
def funnel_note(dm_rows):
    """dm_rows 来自 build_state 的 dm_rows(含 来源笔记/已回/真商机)。
    返回 {by_note:[...], totals:{...}, top:[...]}。"""
    _REPLIED = ("已回", "已自动回")
    _BIZ = ("是", "像", "宜", "真商机")
    by_note = {}
    for r in dm_rows:
        note = (r.get("来源笔记") or "").strip()
        if not note:
            note = "(未标来源)"
        rec = by_note.setdefault(note, {"note": note, "reach": 0, "reply": 0, "biz": 0})
        rec["reach"] += 1
        if (r.get("已回") or "") in _REPLIED:
            rec["reply"] += 1
        if (r.get("真商机") or "").strip() in _BIZ:
            rec["biz"] += 1
    out = []
    for n, rec in by_note.items():
        reach = rec["reach"] or 1
        rec["rate_r"] = round(rec["reply"] / reach, 2)
        rec["rate_b"] = round(rec["biz"] / reach, 2)
        # 综合分: 商机优先(×0.6) + 回复率(×0.4); 商机越多分越高
        rec["score"] = round(rec["rate_b"] * 0.6 + rec["rate_r"] * 0.4, 3)
        out.append(rec)
    out.sort(key=lambda x: x["score"], reverse=True)
    tot_reach = sum(x["reach"] for x in out)
    tot_reply = sum(x["reply"] for x in out)
    tot_biz = sum(x["biz"] for x in out)
    return {"by_note": out,
            "totals": {"notes": len(out), "reach": tot_reach, "reply": tot_reply, "biz": tot_biz,
                       "rate_r": round(tot_reply / tot_reach, 2) if tot_reach else 0,
                       "rate_b": round(tot_biz / tot_reach, 2) if tot_reach else 0},
            "top": out[:5]}   # 取前5做选题回顾


# 评论区命中的意向人群统计(辅助漏斗)
def comment_intent(cmt_rows, biz_words):
    """按评论正文是否命中业务词, 统计意向人次数。"""
    n = 0
    for r in cmt_rows:
        b = (r.get("body") or "") + " " + (r.get("note") or "")
        if any(w in b for w in biz_words):
            n += 1
    return n


if __name__ == "__main__":
    # 自测
    print("== 合规审计(公开) ==")
    print(compliance_audit("私信我领取资料 微信加我 来报名", "public"))
    print("== 合规审计(私信, 报课程价合规) ==")
    print(compliance_audit("初阶班 1499 起, 私信聊", "private")["ok"])
    print(compliance_audit("我加你微信吧", "private")["ok"])
    print("== 风控 ==")
    print(wind_control("add_friend"))
    print(wind_control("send_comment", audit=compliance_audit("加我微信", "public")))
    print("== 漏斗 ==")
    print(funnel_note([
        {"来源笔记": "A笔记", "已回": "已回", "真商机": "是"},
        {"来源笔记": "A笔记", "已回": "", "真商机": ""},
        {"来源笔记": "B笔记", "已回": "已自动回", "真商机": "像"},
    ]))
