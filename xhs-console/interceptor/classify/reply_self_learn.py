#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书回复 · 话术库自学习闭环
================================================================
职责: 用【真实样本】让话术库"自己长肉"。
  输入一批真实留言/评论 → 诊断 → 命中策略键 → 三路择优 → 星级分级
  → 把高星好句【追加到叠加层策略库对应场景】。

流程(闭环):  样本 → diagnose(5信号) → match_strategy(定位场景键)
  → 生成候选好句(标准库基线 / LLM 现场生成 / 场景补全)
  → 统一打分(接住原话 / 情绪出口 / 下一步 / 无红线 / 像人话)
  → 星级分级(★/★★/★★★)
  → 回灌(★★及以上, 去重, 限流)到 classify/reply_self_generated.yaml
  → 策略命中器 match_strategy 自动把叠加层 lines 并入候选, 立即可用

★ 设计原则(呼应"要能不断学习升级, 不同策略用不同话术"):
  * 不破坏源文件: 生成结果写独立叠加层, reply_strategy.yaml 保持纯手工维护+可热编辑。
  * 择优回灌: 每次生成都打分, 只有真优于(或新角度)现有好句才进库, 不是越堆越水。
  * 红线始终把关: 任何候选句必须过红线(公开区最严/私信允许1499), 不过即弃。
  * 可复现: 打分这套规则是确定性的(不依赖 LLM 随机), 同一输入得同一结论。

用法(纯函数 / CLI):
  from reply_self_learn import learn_from_samples
  report = learn_from_samples([{"msg":"带团13天三台车天天5点起","note":"研学导游用AI提效","source_hint":"xueyan_ai"}])
  python reply_self_learn.py --dryrun   # 只诊断+择优, 不写文件(演示)
  python reply_self_learn.py --write    # 诊断+择优+回灌叠加层
  python reply_self_learn.py --show     # 查看叠加层当前内容
"""
from __future__ import annotations

import os, re, sys, json, time, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY_FP = os.path.join(HERE, "reply_self_generated.yaml")

# ---- 可配置参数(热编辑) ----
DEFAULTS = {
    "min_stars_to_feed": 2,       # 至少几星才回灌进库
    "max_lines_per_scene": 12,    # 每个场景好句上限(防臃肿)
    "llm_tries": 3,               # LLM 采样次数, 取分数最高者(修采样不稳定性)
    "llm_enabled": True,          # False=不调LLM(快速浏览样本/纯stdlib+scene兜底), dryrun可关
    "scan_len_min": 12,           # 好句最短字数
    "scan_len_max": 130,          # 好句最长字数
    "provider": "",               # 空=走 reply_compose 默认链 deepseek→glm→minimax
}


# ------------------------------------------------------------------ #
# 1. 复用已有三层(诊断/命中/LLM组合)
# ------------------------------------------------------------------ #
def _load_meta():
    import yaml
    try:
        with open(OVERLAY_FP, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        # 兼容: 顶层裸 dict(残留结构) 也能读出 meta/strategies
        meta = d.get("meta") or {}
        strategies = d.get("strategies") or {}
        # 若顶层直接是 config/version 这种 meta 字段(无包裹), 视为整个 dict 就是 meta
        if not strategies and not meta and any(k in d for k in ("config", "version", "updated", "min_stars_to_feed")):
            meta = d
        return meta, strategies or {}
    except Exception:
        return {}, {}


def _load_config():
    cfg = dict(DEFAULTS)
    meta, _ = _load_meta()
    cfg_meta = meta.get("config") or {}
    # 兼容: 若裸 dict 顶层就是 config 字段, 兜底再读一次
    if not cfg_meta and "config" in meta and isinstance(meta.get("config"), dict):
        cfg_meta = meta["config"]
    for k, v in cfg_meta.items():
        cfg[k] = v
    return cfg


# ------------------------------------------------------------------ #
# 2. 候选好句生成(三路)
# ------------------------------------------------------------------ #
def _cand_std(diag, strat):
    """标准库基线: 从命中策略的现有 lines 里挑【评分最高】那条。
    基准来自人工打磨, 是"当前最佳", 新生成的好句需超过它才值得进库。"""
    best = ""
    best_sc = -1
    for l in strat.get("lines", []):
        sc = score_line(l, diag).get("total", 0)
        if sc > best_sc:
            best_sc, best = sc, l
    return best


def _cand_llm(diag, strat, note="", src_type="", source_hint=""):
    """LLM 现场生成: 复用 reply_compose 的 provider 链(deepseek→glm→minimax)。
    ★ 采样不稳定性修复(2026-09-06): 同一样本 LLM 单次生成, 三次得分可达 51/72/62,
    波动会让"择优回灌"不稳定。改为【多次采样取分数最高者】(默认3次, 可配),
    让择优稳定可复现。只要 engine=='llm' 且过红线就作为候选。"""
    cfg = _load_config()
    llm_on = bool(cfg.get("llm_enabled", True))
    tries = max(1, int(cfg.get("llm_tries", 3)))
    best_text, best_sc = "", -1
    if not llm_on:
        return ""
    try:
        from reply_compose import compose_reply
        for _ in range(tries):
            r = compose_reply(diag.get("raw", ""), note, src_type, source_hint=source_hint)
            if r.get("engine") == "llm" and r.get("reply"):
                sc = score_line(r["reply"], diag)["total"]
                if sc > best_sc:
                    best_sc, best_text = sc, r["reply"]
    except Exception:
        pass
    return best_text


def _cand_scene(diag, strat):
    """场景补全: 基于策略骨架 tone + 对方原话关键词, 确定性拼一条。
    定位=【无 stdlib/LLM 时的兜底角度】, 所以天然该比前两者弱一档。
    主打"复现原话语义 + 情绪出口 + 下一步", 但不再套"像XXX这种"模板腔。"""
    raw = (diag.get("raw") or "").strip()
    mood = diag.get("mood", "neutral")
    stage = diag.get("stage")
    qk = diag.get("quote_kind")
    # 情绪出口
    pref = ""
    if mood in ("anxious", "hesitant"):
        pref = _empathy(mood)
    # 接住原话(化用, 不复读)
    frag = _semantic_frag(raw)
    # 价值点(从骨架 tone 抽)
    value = _tone_value(strat.get("tone", "") or "")
    # 下一步
    nxt = _next_step(diag)
    parts = []
    if pref:
        parts.append(pref)
    if frag and frag not in parts:
        parts.append(frag)
    if value:
        parts.append(value)
    if nxt:
        parts.append(nxt)
    s = "".join(parts)
    # 兜底: 若拼出来太短/太模板(无有效内容), 返回空, 让 stdlib/llm 胜出
    if len(s) < 12 or any(w in s for w in ("这种我", "我常遇到")):
        return ""
    return s


def _semantic_frag(raw):
    """把原话化成一句"接住你话"的短句(不复读原字, 用同义/上义词)。"""
    raw = raw or ""
    if any(w in raw for w in ("累", "辛苦", "熬夜", "早起", "5点")):
        return "带团这强度确实熬人,"
    if any(w in raw for w in ("0基础", "零基础", "小白", "不会")):
        return "零基础反而好带,"
    if any(w in raw for w in ("多少钱", "一单", "单价", "价格", "收费")):
        return "这价格得看单子类型,"
    if any(w in raw for w in ("线下", "见面", "约")):
        return "能线下聊就更直接了,"
    if any(w in raw for w in ("资料", "模板", "教程")):
        return "资料我这边有现成的,"
    if any(w in raw for w in ("担心", "怕", "跟不上")):
        return "这顾虑我懂,"
    return ""


def _empathy(mood):
    if mood == "anxious":
        return "先别急,这种焦虑我见太多了。"
    if mood == "hesitant":
        return "有顾虑太正常了,能理解。"
    return ""


def _next_step(diag):
    stage = diag.get("stage")
    qk = diag.get("quote_kind")
    if stage == "quote":
        if qk == "task_price":
            return "您想先了解接单模式,还是具体能接什么单?"
        return "您是想学AI,还是已经在研学/带团这块想升级?"
    if stage == "act":
        return "咱直接往下聊,您方便的时间我来跟您捋一遍。"
    if stage == "hesitant":
        return "您先说说最担心哪点,我给您个实在建议,不着急。"
    if stage == "value":
        return "您是想直接要能用的,还是先看看哪个方向适合您?"
    return "您方便说下现在的情况吗?"


def _tone_value(tone):
    """从策略骨架里抽一段"我是/我这边"的价值句(粗提取)。"""
    m = re.search(r"[^。]*?(我这边|我是|我带|我做的|我教)[^。]*", tone or "")
    s = (m.group(0) if m else "").strip()
    return s if s else ""


# ------------------------------------------------------------------ #
# 3. 打分(确定性, 0-3 星的总分基座)
# ------------------------------------------------------------------ #
# 情绪/价值/下一步 词库
_EMPATHY_WORDS = ("别急", "理解", "太正常", "我见", "能懂", "太懂了", "不是", "先别", "放松", "难得")
_VALUE_WORDS = ("省", "提效", "直接", "落地", "能接", "上手", "带", "快", "实际", "立刻", "模板", "省事")
_NEXT_WORDS = ("想先", "您先", "您要", "方便", "要不要", "怎么", "哪块", "帮", "您来", "跟我", "聊聊", "看看")
_CORP_WORDS = ("我们团队", "本公司", "官方", "平台", "敬请", "欢迎咨询", "我们提供")
# ★ 模板痕迹: 机械拼凑句的特征(降分), 让"人工打磨句"和"LLM 现场句"优先胜出
_TEMPLATE_MARKS = ("我常遇到", "这种我", "像您", "像这种", "您方便说下", "我见太多了",
                   "太正常了", "能理解。", "我想先了解", "您想先了解")
# ★ 原话复现强度: 真把原话关键词带进回复, 才算"接住"(不是套一句话)
_RAW_COPY = ("13天", "三台车", "5点", "0基础", "初阶班", "一单", "多少钱", "任务", "线下",
             "资料", "钱", "陪", "带团", "导游", "研学", "失眠")
_RED = (  # 复用 reply_compose 的红线(对齐 _RED_PRIVATE 私信通道, 公开区更严交 compose 把关)
    "微信", "wx", "vx", "VX", "薇", "薇信", "微心", "威信", "绿泡泡",
    "企鹅", "添加", "加V", "加v", "+v", "＋v", "➕", "号码", "手机号",
    "手机", "电话", "q号", "qq", "扣扣", "二维码", "扫码", "扫我",
    "1-8", "1?3?5", "131", "138", "4980", "20-60", "元/条",
    "包接单", "包涨薪", "包升学", "包教包会", "包你", "保证接单", "保证涨", "保证赚",
    "稳赚", "躺赚", "绝对", "全网最", "行业第一", "全国第一", "没有之一", "yyds", "无敌",
)
# ★ 导流承接场景(2026-09-06): 对方主动求联/含蓄递联系方式的"承接话术"里,
#   "看主页/关注/置顶/私信我"是【合规承接动作】(小红书官方认可的承接方式, 只禁评论区诱导变现)。
#   用户明确: 导流技巧是最值钱的话术类别, 必须学、不能当垃圾滤。所以这些词在
#   lead_routing 场景下【放行】(私信通道本就放行, 这里显式声明, 防公开区误杀)。
_RED_LEAD_OK = ("看主页", "私信我", "扣1", "加我", "进主页")


def _pass_red(t, allow_lead=False):
    """红线过滤(对齐 compose _RED_PRIVATE 私信通道)。
    allow_lead=True 时(导流承接场景)显式放行"看主页/私信我/扣1/加我"这类合规承接动作,
    但硬红线(微信/手机号/二维码/谐音/旧价/包接单/绝对化)依然一票否决。
    注: 学习闭环默认对齐【私信通道】(放行看主页/私信我); 公开区最严交 compose _PUBLIC_RED 把关。"""
    low = (t or "").lower()
    for b in _RED:
        if b.lower() in low:
            if allow_lead and b in _RED_LEAD_OK:
                continue          # 导流承接场景: 合规动作引导放行
            return False
    return True


def score_line(cand, diag):
    """确定性打分。返回 dict {total, 子分...}, total 0-100。"""
    if not cand:
        return {"total": 0, "len": 0, "overlap": 0, "empathy": 0, "next": 0, "red": 0, "corp": 0}
    c = cand
    total = 0
    sub = {}
    # 长度
    L = len(c)
    if L < 14:
        sub["len"] = -20 if L < 8 else 5
    elif 14 <= L <= 120:
        sub["len"] = 18
    else:
        sub["len"] = 6
    total += sub["len"]

    # 接住原话: 与 raw 的字面重叠 + 阶段语义
    raw = (diag.get("raw") or "")
    sub["overlap"] = 0
    # 真实复现原话关键词(能看出是在"回对方那句话")
    raw_hit = sum(1 for w in _RAW_COPY if w in c)
    if raw_hit:
        sub["overlap"] += min(14, 5 + 4 * raw_hit)
    common = [w for w in re.findall(r"[\u4e00-\u9fa5]{2,4}", raw) if w in c]
    if common:
        sub["overlap"] += min(10, 2 + 2 * len(common))
    # 阶段语义: 命中 quote_kind/act 等关键信号词
    qk = diag.get("quote_kind")
    if qk == "task_price" and ("单" in c or "任务" in c or "接" in c):
        sub["overlap"] += 8
    elif qk == "course_price" and (("1499" in c) or ("课程" in c) or ("班" in c)):
        sub["overlap"] += 8
    total += sub["overlap"]

    # 情绪出口
    mood = diag.get("mood", "neutral")
    sub["empathy"] = 0
    if mood in ("anxious", "hesitant"):
        if any(w in c for w in _EMPATHY_WORDS):
            sub["empathy"] = 16
        else:
            sub["empathy"] = 2  # 焦虑/犹豫必须共情, 没共情扣分
    else:
        sub["empathy"] = 8  # 非情绪场景, 有"接住"即可
    total += sub["empathy"]

    # 下一步
    sub["next"] = 14 if any(w in c for w in _NEXT_WORDS) else 3
    total += sub["next"]

    # 价值度(非必须但加分)
    sub["value"] = 10 if any(w in c for w in _VALUE_WORDS) else 3
    total += sub["value"]

    # ★ 模板痕迹惩罚: 机械拼凑句降分, 别让它压过人工句
    sub["template"] = -22 if any(w in c for w in _TEMPLATE_MARKS) else 0
    total += sub["template"]

    # 红线(一票否决) + 公司腔降级
    # ★ 导流承接场景(2026-09-06): source=lead_routing 时"看主页/私信我"等合规承接动作放行
    is_lead = (diag.get("source") in ("lead_routing",)) or (diag.get("sample_type") == "lead_angle")
    sub["red"] = 0 if _pass_red(c, allow_lead=is_lead) else -60
    total += sub["red"]
    sub["corp"] = -12 if any(w in c for w in _CORP_WORDS) else 0
    total += sub["corp"]

    sub["total"] = max(0, total)
    return sub


def to_stars(score_total):
    if score_total <= 0:
        return 0
    if score_total >= 90:
        return 3
    if score_total >= 72:
        return 2
    if score_total >= 55:
        return 1
    return 0


# ------------------------------------------------------------------ #
# 4. 主入口: 从样本学习
# ------------------------------------------------------------------ #
def learn_from_samples(samples, write=True, console=True):
    """samples: [{msg, note?, src_type?, source_hint?}]。
    返回 report(list[dict]), 每条含 样本/诊断/命中键/三路候选/择优结果/星级。
    write=True 时把 ★★及以上 回灌叠到 OVERLAY_FP。"""
    from reply_diagnose import diagnose
    from reply_strategy import match_strategy

    cfg = _load_config()
    entries = []
    for s in samples:
        msg = s.get("msg", "")
        note = s.get("note", "")
        src_type = s.get("src_type", "")
        source_hint = s.get("source_hint", "")
        diag = diagnose(msg, note, src_type, source_hint=source_hint)
        strat = match_strategy(diag)
        key = strat.get("key") or _fallback_key(diag)

        # 三路候选
        cand_std = _cand_std(diag, strat)
        cand_llm = _cand_llm(diag, strat, note, src_type, source_hint)
        cand_scene = _cand_scene(diag, strat)
        cands = [("stdlib", cand_std), ("llm", cand_llm), ("scene", cand_scene)]

        # 打分择优
        best_label, best_text, best_sc = "", "", -1
        details = []
        for label, text in cands:
            if not text:
                continue
            sc = score_line(text, diag)
            total = sc["total"]
            details.append({"label": label, "text": text, "score": total, "stars": to_stars(total)})
            if total > best_sc:
                best_sc, best_label, best_text = total, label, text
        stars = to_stars(best_sc)

        entries.append({
            "sample": msg,
            "diag": diag,
            "key": key,
            "best": {"label": best_label, "text": best_text, "score": best_sc, "stars": stars},
            "candidates": details,
        })

    if write:
        # 只回灌 ★★ 及以上
        to_feed = [e for e in entries if e["best"]["stars"] >= cfg["min_stars_to_feed"]]
        _feed_overlay(to_feed, cfg)

    return entries


def _fallback_key(diag):
    stage = diag.get("stage", "first_ask")
    source = diag.get("source", "dm_unknown")
    persona = diag.get("persona", "any")
    return f"{stage}__{source}__{persona}"


# ------------------------------------------------------------------ #
# 5. 回灌叠加层
# ------------------------------------------------------------------ #
def _feed_overlay(entries, cfg):
    import yaml
    from reply_strategy import _load_strategy  # 读源库, 判断是不是"新角度"
    try:
        src_stra = _load_strategy().get("strategies", {}) or {}
    except Exception:
        src_stra = {}
    # 读现有叠加层(保留 meta 与已有策略)
    meta, strategies = _load_meta()
    if not meta:
        meta = {"version": "1.0", "updated": time.strftime("%Y-%m-%d %H:%M"),
                "note": "AI 自学习生成的好句叠加层, 源文件 reply_strategy.yaml 保持手工维护",
                "config": dict(cfg)}
    changed = []
    for e in entries:
        key = e["key"]
        txt = e["best"]["text"]
        diag = e["diag"]
        if not txt:
            continue
        # ★ 新角度判断: 与【源库】已有好句或【叠加层】已有句都不显著重复才进, 避免无效复制
        src_lines = (src_stra.get(key) or {}).get("lines", []) or [] if key in src_stra else []
        if any(_similar(txt, x) for x in src_lines):
            continue
        bucket = strategies.setdefault(key, {"lines": []})
        lines = bucket["lines"]
        if any(_similar(txt, x) for x in lines):
            continue
        # 限流: 超过上限则只保留分数高于最差者的
        if len(lines) >= cfg["max_lines_per_scene"]:
            worst_idx = 0
            worst = score_line(lines[0], diag)["total"]
            for i, x in enumerate(lines[1:], 1):
                sx = score_line(x, diag)["total"]
                if sx < worst:
                    worst, worst_idx = sx, i
            if e["best"]["score"] > worst:
                del lines[worst_idx]
            else:
                continue
        lines.append(txt)
        changed.append({"key": key, "text": txt, "stars": e["best"]["stars"]})
    # 写回
    with open(OVERLAY_FP, "w", encoding="utf-8") as f:
        yaml.safe_dump({"meta": meta, "strategies": strategies},
                       f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return changed


def _similar(a, b):
    """判断两句是否显著重复(共同 2-字以上短语)。"""
    if a == b:
        return True
    A = set(re.findall(r"[\u4e00-\u9fa5]{2}", a))
    B = set(re.findall(r"[\u4e00-\u9fa5]{2}", b))
    if not A or not B:
        return False
    overlap = len(A & B)
    return overlap >= 3 or (overlap / min(len(A), len(B))) >= 0.6


# ------------------------------------------------------------------ #
# 6. 展示/CLI
# ------------------------------------------------------------------ #
def show_report(entries, verbose=True):
    for e in entries:
        d = e["diag"]
        b = e["best"]
        print(f"\n== 样本: {e['sample']!r}")
        print(f"  诊断: stage={d['stage']} source={d['source']} persona={d['persona']} mood={d['mood']} qk={d.get('quote_kind')}")
        print(f"  场景键: {e['key']}")
        for c in e["candidates"]:
            tag = "★" * c["stars"] if c["stars"] else "—"
            mk = "✓" if c["label"] == e["best"]["label"] else " "
            print(f"    {mk}[{c['label']:6}] {c['score']:3} {tag} {c['text'][:56]}")
        if b["text"]:
            print(f"   >>> 最佳({b['label']},{b['score']}分,{b['stars']}星): {b['text']}")


def show_overlay():
    meta, strategies = _load_meta()
    print("=== 叠加层 meta ===")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print("\n=== 叠加层 strategies ===")
    for k, v in strategies.items():
        print(f"  [{k}]")
        for l in v.get("lines", []):
            print(f"     - {l}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    mode = "--show" if "--show" in argv else ("--write" if "--write" in argv else "--dryrun")

    if mode == "--show":
        show_overlay()
        sys.exit(0)

    # 内置一组演示样本(真实性的代表样本, 可换成真采集)
    demo = [
        {"msg": "带团13天三台车天天5点起来看资料，太累了", "note": "研学导游怎么用AI提效", "source_hint": "xueyan_ai"},
        {"msg": "不能0基础 我qi你", "note": "示例笔记", "source_hint": "opc_recruit"},
        {"msg": "可以支持线下 我想看看是那种任务类型 多少钱一单", "note": "", "source_hint": "opc_recruit"},
        {"msg": "你们初阶班多少钱怎么报名", "note": "", "source_hint": "opc_recruit"},
        {"msg": "有怕跟不上 白花钱吗", "note": "", "source_hint": "ai_training"},
        {"msg": "求资料", "note": "示例笔记", "source_hint": "opc_recruit"},
    ]
    res = learn_from_samples(demo, write=(mode == "--write"))
    show_report(res)
    if mode == "--write":
        meta, _ = _load_meta()
        print(f"\n>>> 已回灌到 {os.path.basename(OVERLAY_FP)}")
        print("    show_overlay() 可查看; match_strategy 已自动并入候选。")
