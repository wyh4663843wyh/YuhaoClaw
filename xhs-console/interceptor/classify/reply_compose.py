#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书回复 · LLM 适配层(组合润色)
================================================================
职责: 把「诊断信号 + 策略骨架 + 候选好句 + 对方原话」组合成一句贴合回复。
  优先走 LLM(可插拔): 把上下文拼进 prompt, 让模型按场景选/组合/润色。
  无 LLM key 或调用失败 → 规则兜底: 从候选好句按场景直接选一条+微调,
    保证【永远能出结果、可热编辑、不依赖外部 key】。

★ 多 provider 支持(REPLY_LLM_PROVIDER):
  deepseek → deepseek-v4-flash-vision-exp (https://api.deepseek.com)
  zhipu/glm → glm-5.3-flash        (https://open.bigmodel.cn/api/coding/paas/v4) ★Coding Plan
  minimax  → MiniMax-M3            (https://api.minimaxi.com/v1)  ← 仅兜底
  ★ 默认链(未显式指定时): deepseek → glm(zhipu) → minimax
    逐 provider 生成: 有 key 的按序尝试; 个别 provider 偶发被红线过滤/返回空
    → 自动换下一个; 全失败 → 规则兜底 → 空(待人工)。
  key 从环境变量或 ~/.baoyu-skills/.env 读取(按 provider 的 key_env 名)。
  REPLY_LLM_PROVIDER 可强制指定某一个(只走它, 也保留规则兜底)。

★ 设计原则(呼应宇豪"要能不断学习升级, 不同策略用不同话术"):
  * 策略库是"骨架+好句", LLM/规则是"把骨架穿成贴人话的那一句"
  * 关键约束: 一定接住对方原话里的【事实/关键词/阶段】, 不能再答非所问
  * 红线: 不出现微信/联系方式/谐音导流/绝对化词/旧OPC价/任务单价

用法(纯函数):
  from reply_compose import compose_reply
  reply = compose_reply(msg="多少钱一单", note="示例笔记", src_type="OPC接单")
    → "您问的是'接一单能拿多少'对吧? 这个得看单子类型..."  (规则兜底或LLM润色)
"""
from __future__ import annotations

import os, re

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- LLM 可插拔配置(多provider, 读环境变量, 缺省即用规则兜底) ----
# 优先级: 环境变量 REPLY_LLM_KEY/REPLY_LLM_BASE/REPLY_LLM_MODEL 优先
#         > ~/.baoyu-skills/.env 里对应 provider 的 key
# 选择器: REPLY_LLM_PROVIDER = minimax | deepseek | zhipu
#         (缺省 minimax, 兼容旧版; 环境变量没有时自动探测 .env)
_PROVIDERS = {
    "minimax": {
        "key_env": "MINIMAX_API_KEY",          # 环境变量名(也去 .env 找)
        "base_default": "https://api.minimaxi.com/v1",
        "model_default": "MiniMax-M3",
    },
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "base_default": "https://api.deepseek.com",
        "model_default": "deepseek-v4-flash-vision-exp",
    },
    "zhipu": {  # 智谱 GLM
        "key_env": "ZHIPU_API_KEY",            # 兼容 ZAI_API_KEY / BIGMODEL_API_KEY
        "base_default": "https://open.bigmodel.cn/api/coding/paas/v4",   # ★Coding Plan 套餐专属
        "model_default": "glm-5.3-flash",
    },
}
# 兼容旧别名
_ALIASES = {"zai": "zhipu", "bigmodel": "zhipu", "glm": "zhipu"}

# ★ LLM 首选链(优先级从高到低): deepseek → glm(zhipu) → minimax(兜底)
#   * 用户明确: minimax 只用兜底, 尽量不用(模型不如前两个强)
#   * 链逻辑: 逐个 provider 找 key, 有 key 的第一个当默认; 全无 key → 空(走规则兜底)
_DEFAULT_CHAIN = ("deepseek", "zhipu", "minimax")


def _normalize_provider(name):
    n = (name or "").strip().lower()
    return _ALIASES.get(n, n if n in _PROVIDERS else "minimax")


def _load_env_key(key_env):
    """从 .env 读取指定 key 名(如 DEEPSEEK_API_KEY)。
    兼容同一 provider 的多个别名环境变量名。读不到返回空(走规则兜底)。
    .env 路径可用环境变量 XHS_ENV_FILE 覆盖(默认 ~/.baoyu-skills/.env, 按你实际部署改)。"""
    import os
    # 备选 key 名(同一 provider 常见别名)
    aliases = {
        "MINIMAX_API_KEY": ["MINIMAX_API_KEY"],
        "DEEPSEEK_API_KEY": ["DEEPSEEK_API_KEY"],
        "ZHIPU_API_KEY": ["ZHIPU_API_KEY", "ZAI_API_KEY", "BIGMODEL_API_KEY"],
    }.get(key_env, [key_env])
    ep = os.path.expanduser(os.environ.get("XHS_ENV_FILE", "~/.baoyu-skills/.env"))
    lines = []
    try:
        with open(ep, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f]
    except Exception:
        pass
    for name in aliases:
        # 先环境变量
        v = os.environ.get(name, "").strip()
        if v:
            return v
        # 再 .env
        for ln in lines:
            if ln.startswith(name + "="):
                return ln.split("=", 1)[1].strip()
    return ""


def _env(name, dflt=""):
    v = os.environ.get(name, "")
    return v if v else dflt


def _load_env_provider():
    """从 .env 读 REPLY_LLM_PROVIDER(持久化 provider 选择)。
    优先级: 环境变量 REPLY_LLM_PROVIDER > .env 的 REPLY_LLM_PROVIDER > 空(走默认链)。
    .env 路径可用环境变量 XHS_ENV_FILE 覆盖(默认 ~/.baoyu-skills/.env)。"""
    import os
    v = os.environ.get("REPLY_LLM_PROVIDER", "").strip()
    if v:
        return v
    ep = os.path.expanduser(os.environ.get("XHS_ENV_FILE", "~/.baoyu-skills/.env"))
    try:
        with open(ep, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("REPLY_LLM_PROVIDER="):
                    return ln.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _resolve_llm():
    """解析 LLM 配置: 返回 (provider, key, base, model)。
    优先级:
      1) REPLY_LLM_PROVIDER 显式指定 → 只用它(找不到 key 则空, 走规则兜底)。
      2) 未指定 → 按首选链 _DEFAULT_CHAIN(deepseek→zhipu→minimax)
         逐个探测 key, 用第一个有 key 的 provider。
    默认链把 minimax 排最后, 符合用户「deepseek/glm 优先, minimax 只兜底」的要求。"""
    # 1) 用户显式指定 provider(环境变量 > .env)
    explicit = _load_env_provider()
    if explicit:
        provider = _normalize_provider(explicit)
        p = _PROVIDERS[provider]
        key = _env("REPLY_LLM_KEY", _load_env_key(p["key_env"]))
        base = _env("REPLY_LLM_BASE", p["base_default"])
        if not base and explicit in ("zhipu", "glm", "zai", "bigmodel"):
            base = _env("ZHIPU_API_BASE", p["base_default"])
        model = _env("REPLY_LLM_MODEL", p["model_default"])
        return provider, key, base, model

    # 2) 首选链探测: 依次找有 key 的 provider
    for prov in _DEFAULT_CHAIN:
        p = _PROVIDERS[prov]
        key = _load_env_key(p["key_env"])
        if not key:
            continue
        base = _env("REPLY_LLM_BASE", p["base_default"])
        # Coding Plan 套餐 key 需配专属 base(从 .env 的 ZHIPU_API_BASE 读, 否则用默认)
        if prov == "zhipu":
            base = _env("ZHIPU_API_BASE", p["base_default"])
        model = _env("REPLY_LLM_MODEL", p["model_default"])
        return prov, key, base, model

    # 3) 全无 key → 返回空(上层走规则兜底)
    return "deepseek", "", _PROVIDERS["deepseek"]["base_default"], _PROVIDERS["deepseek"]["model_default"]


LLM_PROVIDER, LLM_KEY, LLM_BASE, LLM_MODEL = _resolve_llm()


# ---------------------------------------------------------------- 红线过滤
# 生成内容必须过这道门: 命中即降级(绝不放雷)。绝对化词限定为"承诺性"表达,
# 避免单字('最'/'第一')误杀"最省力/最适合"这种正常词汇。
#
# ★ 场景感知红线(2026-09-05 语义修复):
#   * _RED_PRIVATE(私信回复): 对方已主动询价/沟通, 属正常经营场景。
#     → 允许出现 1499/报名/课程/报名费(用户授权"1499元起"明示; 私信报课程价不属导流违规)。
#     → 仍禁: 联系方式/谐音导流/绝对化承诺/旧OPC价4980/任务单价(20-60元条)/包接单等。
#   * _PUBLIC_RED(公开区评论区): 【任何时候最严】, 在 _RED_PRIVATE 基础上再禁止
#     报价卖课词(1499/报名/课程)、营销极限词。公开区只种草/提问, 绝不卖课/导流/留价。
#   * 判断依据: 小红书《交易导流违规管理细则》(2025-03-12) + 2026《社区规范》
#     一评论区"私信我/加我"优先监控; 谐音变体(绿泡泡/VX/薇❤/➕)视作刻意规避从重;
#     联系方式(手机号/二维码)直接违规。且平台对【私信内容同样监控导流词】,
#     故私信里提到微信/手机号/扫码仍属违规, 只是"报课程价"本身合规。
_RED_PRIVATE = (
    # —— 联系方式(私信里提及微信/手机号/扫码/谐音变体仍违规) ——
    "微信", "wx", "vx", "VX", "薇", "微心", "薇信", "威信", "绿泡泡",
    "企鹅", "添加", "加V", "加v", "+v", "＋v", "➕", "号码", "手机号",
    "手机", "电话", "q号", "qq", "扣扣", "二维码", "扫码", "扫我",
    "1-8", "1?3?5", "1-3-5", "131", "138",
    # —— 业务红线(始终禁: 旧OPC价/任务单价/包接单/绝对化) ——
    #   注: "私信我/看主页/加我"等导流动作只在【公开区】违规, 私信区属正常承接, 故放 _PUBLIC_RED。
    "20-60", "元/条", "包接单", "包涨薪", "包升学", "包教包会", "包你",
    "保证接单", "保证涨", "保证赚", "稳赚", "躺赚", "绝对", "4980",
    "全网最", "行业第一", "全国第一", "没有之一", "yyds", "无敌",
)
# 公开区评论区 = 私信红线 + 报价卖课词 + 营销极限词 + 公开导流动作(最严)
#   ★ "私信我/看主页/加我/扣1/领资料" 在【公开区】是平台优先监控的导流话术, 必拦;
#     但在【私信区】属对方咨询后的正常承接, 故只在 _PUBLIC_RED, 不进 _RED_PRIVATE。
_PUBLIC_RED = _RED_PRIVATE + (
    "报名", "课程", "报名费", "1499",
    "最划算", "赔本", "名额", "快抢", "速来",
    # —— 公开区导流动作(评论区"扣1/看主页/领资料/私信我"优先监控, 从重) ——
    "私信我", "私聊我", "私我", "找我聊",
    "进主页", "看主页", "主页", "戳我主页",  "主页链接",
    "扣1", "扣字", "扣关键词", "扣评论", "评论区扣",  "扣码",
    "领资料", "领模板", "领取", "找我要", "找我拿",
    "我发你", "发给你", "给你发", "加我",
)


def _pass_red(text, channel="private"):
    """按通道过滤红线。channel: 'private'(私信回复, 默认) | 'public'(公开区评论区)。"""
    low = (text or "").lower()
    if channel == "public":
        return not any(b.lower() in low for b in _PUBLIC_RED)
    return not any(b.lower() in low for b in _RED_PRIVATE)


def _strip_think(text):
    """剥离 DeepSeek/MiniMax 等推理模型的 <think>...</think> 块。
    推理块里常出现禁词(模型在自我推演时提到'20-60/4980'等), 若不剥离会造成正文被误杀。
    只剥离保留正文; 若剥离后为空, 返回原文本(交给红线再判)。"""
    if not text:
        return text
    # 去掉 <think>...</think> 整体
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    # 部分模型只有 <think> 开头无闭合(截断), 也去掉开头到结尾的残留
    if "<think>" in t:
        t = re.sub(r"<think>.*", "", t, flags=re.S)
    t = t.strip().strip("\"'").strip()
    return t


# ---------------------------------------------------------------- 规则兜底(核心)
def _rule_compose(diag, strat):
    """无 LLM 或失败: 从候选好句里挑【第一条能过红线】的用。
    策略库按 阶段×来源×人群×情绪 分好, 命中器取到的那组就是该场景话术;
    这里逐条过滤红线, 防止个别句子误触绝对化词/导流词。全过不了 → 空(交人工)。"""
    lines = strat.get("lines") or []
    for l in lines:
        if _pass_red(l, channel="private"):   # 私信回复走 private 通道(允许1499/报名)
            return l
    return ""


# ---------------------------------------------------------------- LLM 组合润色
def iter_llm_chain():
    """生成 (provider, key, base, model) 遍历链上所有可用 provider。
    顺序: 默认链 _DEFAULT_CHAIN(deepseek→glm→minimax) 中 有 key 的那些;
      每个 provider 用自己那份 key/base(model 可被 REPLY_LLM_MODEL 覆盖)。
    供 compose_reply 逐个尝试, 实现「deepseek 失败→换 glm→再 minimax」降级。"""
    # 若显式指定了 REPLY_LLM_PROVIDER, 只走那一个
    explicit = _load_env_provider()
    if explicit:
        prov = _normalize_provider(explicit)
        p = _PROVIDERS[prov]
        key = _env("REPLY_LLM_KEY", _load_env_key(p["key_env"]))
        base = _env("REPLY_LLM_BASE", p["base_default"])
        if prov == "zhipu":
            base = _env("ZHIPU_API_BASE", p["base_default"])
        model = _env("REPLY_LLM_MODEL", p["model_default"])
        if key:
            yield (prov, key, base, model)
        return
    # 否则按默认链, 依次 yield 有 key 的
    for prov in _DEFAULT_CHAIN:
        p = _PROVIDERS[prov]
        key = _load_env_key(p["key_env"])
        if not key:
            continue
        base = _env("REPLY_LLM_BASE", p["base_default"])
        if prov == "zhipu":
            base = _env("ZHIPU_API_BASE", p["base_default"])
        model = _env("REPLY_LLM_MODEL", p["model_default"])
        yield (prov, key, base, model)


def _llm_compose(diag, strat, provider="", key=None, base=None, model=None):
    """调 LLM(OpenAI 兼容)组合润色。失败返回 None, 由调用方降级到下一个 provider 或规则。
    支持按 provider 注入各自的 key/base/model; 缺省用全局(默认链解析结果)。"""
    if not key:
        key = LLM_KEY
    if not base:
        base = LLM_BASE
    if not model:
        model = LLM_MODEL
    if not key:
        return None
    try:
        import urllib.request, json
        sys_prompt = (
            "你是小红书私信/评论回复助手。根据下面给出的【场景诊断】【策略骨架】【候选好句】【对方原话】, "
            "写一句贴合对方的回复。要求:\n"
            "1. 一定先接住对方原话里的关键词/事实/阶段, 别答非所问; 尤其对方已说'有经验/直接接单/多少钱一单', "
            "别再问一遍背景、别答错题(问任务价别答课程价)。\n"
            "2. 人话, 像真人聊天, 别客服腔/别'我们团队是'那种官方腔。\n"
            "3. 情绪基调贴对方(焦虑就先共情, 急切就直接给价值)。\n"
            "4. 末尾给一个轻量下一步(可选问/可约/可看), 但别硬卖、别逼单。\n"
            "5. 只输出那一句回复本身, 不要解释、不要列表、不要引号包裹。\n"
            "6. 红线: 绝不出现 微信/手机号/二维码/扫码/谐音导流(绿泡泡/VX/薇/➕)/绝对化承诺词。\n"
            "   注意: 对方已主动询价/要在做业务 → 【可以】报初阶班价格(1499起), "
            "但绝不允许出现旧价4980或任务单价(20-60元/条), 也不堆全部价格档。\n"
        )
        user_prompt = (
            f"【场景诊断】意图:{diag.get('intent')} | 级别:{diag.get('tier')} | "
            f"对话阶段:{diag.get('stage')} | 问价类型:{diag.get('quote_kind')} | "
            f"来源:{diag.get('source')} | 人群:{diag.get('persona')} | 情绪:{diag.get('mood')}\n"
            f"【策略骨架】{strat.get('tone','')}\n"
            f"【候选好句】\n" + "\n".join(f"- {l}" for l in strat.get("lines") or []) + "\n"
            f"【对方原话】{diag.get('raw','')}\n"
            f"请写那一条回复。"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_completion_tokens": 800,
        }
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        txt = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        txt = (txt or "").strip().strip("\"'")
        txt = _strip_think(txt)  # 先剥离 <think>...</think> 推理块(模型会把禁词写进推理, 误杀正文)
        if txt and _pass_red(txt, channel="private"):
            return txt
        return None
    except Exception:
        return None


# ---------------------------------------------------------------- 主出口
def compose_reply(message, note="", src_type="", source_hint=""):
    """输入对方留言 → 输出贴合回复。这是给 Console / auto_reply / classify 的入口。
    降级链: 首选链各 provider 的 LLM(deepseek→glm→minimax) 逐个尝试,
           个别 provider 偶发被红线过滤/返回空 → 自动换下一个; 全失败 → 规则兜底;
           规则也没合适句 → 返回空(待人工)。
    source_hint: 当前赛道视角(如 opc_recruit/xueyan_ai), 传给 diagnose, 仅当
                 文本归因不出来源时用于兜底(赛道切换影响承接话术视角)。"""
    from reply_diagnose import diagnose
    from reply_strategy import match_strategy

    diag = diagnose(message, note, src_type, source_hint=source_hint)
    strat = match_strategy(diag)
    strat_key = strat.get("key")

    # 1) LLM 优先: 遍历链上所有 provider, 逐个用"自己那份 key/base/model"生成
    for prov, key, base, model in iter_llm_chain():
        if not key:
            continue
        llm = _llm_compose(diag, strat, provider=prov, key=key, base=base, model=model)
        if llm:
            return {"reply": llm, "engine": "llm", "diag": diag, "key": strat_key,
                    "provider": prov}
    # 2) 规则兜底
    rule = _rule_compose(diag, strat)
    if rule and _pass_red(rule, channel="private"):
        return {"reply": rule, "engine": "rule", "diag": diag, "key": strat_key,
                "provider": ""}
    # 3) 全空 → 待人工
    return {"reply": "", "engine": "manual", "diag": diag, "key": strat_key,
            "provider": ""}


if __name__ == "__main__":
    tests = [
        ("有aigc制作经验", "示例笔记", "OPC接单"),
        ("直接接单", "示例笔记", "OPC接单"),
        ("可以的，可以直接接单吗", "示例笔记", "OPC接单"),
        ("可以支持线下 我想看看是那种任务类型 多少钱一单", "", ""),
        ("你们初阶班多少钱怎么报名", "", ""),
        ("求资料", "", ""),
        ("是做什么呢", "", ""),
        ("多少钱一单", "", ""),
    ]
    for msg, note, st in tests:
        r = compose_reply(msg, note, st)
        print(f"== {msg!r} [{r['engine']}] key={r['key']}")
        print(f"   → {r['reply'][:70] if r['reply'] else '(待人工)'}")
