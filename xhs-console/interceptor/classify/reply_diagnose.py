#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书回复 · 场景诊断器
================================================================
职责: 输入一条留言/私信原文, 输出 5 个信号 dict:
  {
    "tier"      : "P0/P1/P2/P3"            意图分级(复用 intent_map 规则)
    "intent"    : "意图名"                  命中意图
    "stage"     : "first_ask/confirm/value/quote/hesitant/act"  对话阶段
    "quote_kind": "course_price/task_price/None"  若是问价, 问的是课程价还是任务单价!
    "source"    : "opc_recruit/xueyan_ai/tool_public/comment/dm_unknown" 来源
    "persona"   : "instigator/guide/newbie/operator/unknown" 人群
    "mood"      : "anxious/hesitant/eager/polite/guarded/neutral"  情绪
    "raw"       : 原始文本(供策略层/LLM用)
  }

★ 本项目旧链路最致命的三个坑, 诊断器必须解决:
  1. 阶段错位: 对方已"直接接单/多少钱一单", 结果还套"问背景"模板 → 阶段要精准
  2. 无视原话: 对方明说"有AIGC经验", 模板还问"你是做AI还是入门" → 只认关键词, 不认对方已给的信息
  3. 语义偏题: "多少钱一单" 问的是【任务单价】, 却答成"初阶班1499课程价" → 必须区分 quote_kind!

用法(纯函数, 不依赖真机/网络):
  from reply_diagnose import diagnose
  diag = diagnose("可以支持线下 我想看看是那种任务类型 多少钱一单")
    → {"tier":"P1","stage":"quote","quote_kind":"task_price", ...}
"""
from __future__ import annotations

import os, re

HERE = os.path.dirname(os.path.abspath(__file__))
INTENT_FP = os.path.join(HERE, "intent_map.yaml")
SRC_FP = os.path.join(HERE, "..", "console", "strategy.yaml")


# ------------------------------------------------------------------ #
# 1. 意图分级(复用 intent_map.yaml 规则, 与旧 classify 一致)
# ------------------------------------------------------------------ #
def _load_intents():
    import yaml
    try:
        with open(INTENT_FP, encoding="utf-8") as f:
            return yaml.safe_load(f)["intent"]
    except Exception:
        return []


def classify_intent(text):
    """关键词命中优先级, 返回 (意图dict 或 None)。"""
    intents = _load_intents()
    if not text:
        return None
    for it in intents:
        for kw in it.get("keywords", []):
            if kw and kw in text:
                return it
    return None          # 不命中→"其他/待分类"交给调用方


# ------------------------------------------------------------------ #
# 2. 对话阶段检测(关键: 要"听话", 不能只认固定 cue)
# ------------------------------------------------------------------ #
# 报价阶段细分: 问的是"课程价" 还是 "任务单价/接单价"(这俩回答完全不同!)
_COURSE_PRICE_WORDS = ("课程", "初阶班", "学费", "报班", "多少钱", "价格", "怎么收费",
                       "费用", "报名费", "班")
_TASK_PRICE_WORDS = ("多少钱一单", "一单", "单价", "一单多少钱", "一单多少", "任务价",
                     "接一单", "单子多少钱", "怎么算钱", "结算")

# 明确已进入"能接单/线下/约时间" → act
_ACT_WORDS = ("能接单", "直接接单", "可以接单", "线下", "约时间", "约一下",
              "见面聊", "怎么联系", "加你", "我发你", "留个电话", "你微信")

# 已走到"要资料/想做/想学" → value
_VALUE_WORDS = ("求", "要资料", "想学", "想做", "想了解", "入门", "想入行",
                "感兴趣", "适合我吗", "零基础", "0基础", "能带吗", "怎么学", "提效",
                "想用AI", "用AI", "学AI", "接单", "怎么用", "省事", "效率",
                "不会", "能学吗", "能不能学", "可以学吗")

# 合作/团队信号 → value(× instigator 人群)
_COOP_WORDS = ("合作", "团队", "讲师", "我们一起", "一起做", "机构", "旅行社",
               "找人", "招人", "搭班子", "合伙", "加盟", "师资")

# 明确说自己在做/有基础 → confirm(背景确认, 别再问一遍!)
_CONFIRM_WORDS = ("有", "经验", "做过", "在做", "在带", "有团队", "一行话术常用",
                  "我是", "我们团队", "讲师", "导游", "地接", "已经")

# 犹豫/权衡 → hesitant
_HESITANT_WORDS = ("犹豫", "担心", "怕", "再看看", "对比", "考虑", "怕踩坑",
                   "靠谱吗", "真的吗", "值吗", "靠谱", "要不要", "观望", "纠结")

# 焦虑 → anxious
_ANXIOUS_WORDS = ("焦虑", "焦虑了", "很累", "累死", "失眠", "赔钱", "被卷",
                  "太难", "没流量", "没头绪", "好难")


def detect_stage(text):
    """返回 (stage, quote_kind)。
    stage: first_ask/confirm/value/quote/hesitant/act
    quote_kind: course_price/task_price/None (仅 stage==quote 时有意义)
    优先级从高到低: quote 最优先(且先分课程价/任务价), 其次 act, 再 value,
    confirm, hesitant/anxious(情绪), 最后 first_ask。
    """
    t = _norm(text)
    # 0) 先判"报价"——最容易答错题, 必须最先处理
    if _has_any(t, _ACT_WORDS) and _has_any(t, ("多少钱", "价格", "收费", "一单", "单价")):
        # "多少钱一单/单价" 属任务价; "课程多少钱" 属课程价
        qk = "task_price" if _has_any(t, ("一单", "单价", "多少钱一单", "单子")) else "course_price"
        return "quote", qk
    if _has_any(t, _COURSE_PRICE_WORDS):
        # 区分: 含"一单/单价/接单"字样偏向任务价
        qk = "task_price" if _has_any(t, ("一单", "单价", "单子", "接单")) else "course_price"
        return "quote", qk
    if _has_any(t, _TASK_PRICE_WORDS):
        return "quote", "task_price"
    # 1) act: 明确要接单/线下/约时间
    if _has_any(t, _ACT_WORDS):
        return "act", None
    # 2) 情绪优先: 犹豫/焦虑 要先共情, 不该被 confirm 的"有x经验"抢走
    #    (修复: "怕被割韭菜/有点担心" 这种强情绪词, 优先于 confirm 归 hesitant)
    if _has_any(t, _HESITANT_WORDS) or _has_any(t, _ANXIOUS_WORDS):
        return "hesitant", None
    # 3) 合作/团队信号(讲师/机构/合作/团队/一起做) → value 直接给合作切入
    #    (修复: "我们有团队想找讲师合作" 不该被 confirm 抢走, 归 value+instigator)
    if _has_any(t, _VALUE_WORDS) or _has_any(t, _COOP_WORDS):
        return "value", None
    # 4) confirm: 对方在说自己的情况/有经验
    #    注意: "有aigc制作经验" → confirm; "是做什么呢" 这种不了解 → first_ask
    if _has_any(t, _CONFIRM_WORDS) and not _has_any(t, ("是做什么", "什么", "干嘛", "怎么搞")):
        return "confirm", None
    # 5) 兜底 first_ask
    return "first_ask", None


def _has_any(t, words):
    return any(w and w in t for w in words)


# ------------------------------------------------------------------ #
# 3. 来源检测(决定切入角度) — 优先级由上层传入 note/src 辅助, 此处文本兜底
# ------------------------------------------------------------------ #
# 导流/联系方式 信号(对方主动求联 或 在含蓄递联系方式 → 独立高频场景, 走专属策略桶)
# ★ 2026-09-06 权威: 导流技巧是"最值钱"的话术类别(平台对导流监控严,发多了被屏蔽,
#   "怎么含蓄/不违规地递接联系方式"本身是高价值)。所以 lead_routing 作为独立 source 识别,
#   让学习闭环把这类技巧回灌到专属桶(而非淹没在通用首询里)。
_LEAD_PAT = re.compile(
    r"(\d{5,}|1[3-9]\d{9}|[Ww][Xx]|[Qq]{2}|扣扣|qq|二维码|扫我|扫码|加v|加微|加薇|加ㄨ|"
    r"薇[信心]|微[信心]|威信|绿泡泡|VX|vx|Vx|＋v|\+\s?v|1[3-9][- .]?\d{2,4}[- .]?\d{4}|"
    r"lian|联[系合]|联系我|找我|私我|私我聊|V我|＋我|[+\+➕＋]\s?(v|V|v信)|"
    r"加个|加一|留一下|留个|发我|给你发|我给你|我发你|后台|主页|简介|置顶|看主页|看简介|"
    r"[➕＋]|威信|📮|✉|Telegram|电报|(?<![a-z])tg(?![a-z])|(?<![a-z])ins(?![a-z])|"
    r"(?<![a-z])ig(?![a-z])|怎么联系|约一下|我加你|加个微信|"
    r"1(?:\D?\d){6,}\D?)", re.I)


def detect_source(text, note="", src_type="", source_hint=""):
    t = _norm(text) + " " + _norm(note) + " " + _norm(src_type)
    # ★ 0) 导流/联系方式优先(最值钱场景): 命中即 lead_routing, 不被赛道词淹没
    #    (对方在留联系方式/含蓄递联系方式, 是要学"怎么不违规接/递"的技巧, 不是接单/研学咨询)
    if _LEAD_PAT.search(text):
        return "lead_routing"
    # 已有来源类型(采集器归因) → 直接映射
    if "OPC" in t or "接单" in t or "承接" in t:
        return "opc_recruit"
    if "研学" in t or "带团" in t or "地接" in t:
        return "xueyan_ai"
    if "培训" in t or "课程" in t or "考证" in t or "认证" in t or "技能" in t or "职业" in t:
        return "ai_training"
    if "AI表格" in t or "表格" in t or "干货" in t or "工具" in t:
        return "tool_public"
    if "评论" in t or "@" in note:
        return "comment"
    # 文本归因不出确定来源 → 用赛道 source_hint 兜底(如有)
    #   场景: 对方只回"就这/多少钱/随便问问"这类无信息量短句, 赛道决定切入视角
    #   (opc=接单变现, yanxue=研学落地, ai_training=AI技能培训)
    if source_hint and source_hint in ("opc_recruit", "xueyan_ai", "ai_training", "tool_public", "comment", "dm_unknown"):
        return source_hint
    return "dm_unknown"


# ------------------------------------------------------------------ #
# 4. 人群检测(决定口吻/给什么)
# ------------------------------------------------------------------ #
_PERSONA_PAT = {
    "instigator": ("讲师", "老师", "团队", "合伙人", "机构", "我们是"),
    "guide":      ("导游", "地接", "带团", "研学导师", "领队", "研学导游"),
    "newbie":     ("零基础", "0基础", "小白", "入门", "想学", "没基础", "新手", "刚入行", "不会"),
    "operator":   ("运营", "自媒体", "做号", "内容", "打工", "上班", "提效"),
}


def detect_persona(text):
    t = _norm(text)
    for p, kws in _PERSONA_PAT.items():
        if _has_any(t, kws):
            return p
    return "unknown"


# ------------------------------------------------------------------ #
# 5. 情绪检测(决定共情前置还是方案前置)
# ------------------------------------------------------------------ #
def detect_mood(text, stage):
    t = _norm(text)
    if _has_any(t, _ANXIOUS_WORDS):
        return "anxious"
    if _has_any(t, ("犹豫", "担心", "怕", "再看看", "对比", "值吗", "靠谱吗")):
        return "hesitant"
    if stage in ("act", "quote"):
        # 已明确要接单/问价 → 多为主动型
        return "eager"
    if _has_any(t, ("谢谢", "你好", "请问", "麻烦")):
        return "polite"
    # 防营销: 一击纯推销/超短/无情绪词 → guarded 交给策略层谨慎处理
    if _is_guarded(t):
        return "guarded"
    return "neutral"


def _is_guarded(t):
    # 命中广告/推销词, 或极短且无信息量 → 防营销
    ad_kw = ("代发", "网红", "加v", "vx", "绿泡泡", "点赞互", "互粉", "代运营",
             "办理证书", "招商", "加盟", "批量", "经销")
    if _has_any(t, ad_kw):
        return True
    return False


# ------------------------------------------------------------------ #
# 6. 出口
# ------------------------------------------------------------------ #
# 规范化: 去空白/方向符, 全角逗号处理, 小写便于英文关键词
def _norm(s):
    s = (s or "").replace("\u200e", "").replace("\u200f", "").strip()
    if not s:
        return ""
    return s.replace("，", ",")


def diagnose(text, note="", src_type="", source_hint=""):
    """主入口: 返回 5 信号 dict。纯函数, 无副作用。
    source_hint: 当前赛道视角(如 opc_recruit/xueyan_ai), 仅当文本归因不出来源时兜底。"""
    text = text or ""
    intent = classify_intent(text)
    tier = (intent.get("tier") if intent else "P3")
    intent_name = (intent.get("name") if intent else "其他/待分类")
    stage, quote_kind = detect_stage(text)
    source = detect_source(text, note, src_type, source_hint=source_hint)
    persona = detect_persona(text)
    mood = detect_mood(text, stage)
    return {
        "tier": tier, "intent": intent_name,
        "stage": stage, "quote_kind": quote_kind or "None",
        "source": source, "persona": persona, "mood": mood,
        "raw": text,
    }


if __name__ == "__main__":
    import json
    tests = [
        # (留言, note, 期望关键信号)  用真实留言验证
        ("有aigc制作经验", "示例笔记", "confirm"),
        ("是做什么呢", "", "first_ask"),
        ("直接接单", "示例笔记", "act"),
        ("可以的，可以直接接单吗", "示例笔记", "act"),
        ("可以支持线下 我想看看是那种任务类型 多少钱一单", "", "quote"),
        ("你们初阶班多少钱怎么报名", "", "quote"),
        ("求资料", "", "value"),
        ("多少钱一单", "", "quote"),
    ]
    for msg, note, exp_stage in tests:
        d = diagnose(msg, note)
        ok = "✓" if d["stage"] == exp_stage else "✗"
        print(f"{ok} stage={d['stage']:<10} qk={d['quote_kind']:<12} tier={d['tier']}  {msg!r}")
