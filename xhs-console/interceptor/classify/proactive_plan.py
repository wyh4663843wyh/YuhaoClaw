#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书 主动留言+截流 · 内容规划层(纯函数, 不依赖真机)
================================================================
职责:
  * 读 proactive_targets.yaml 配置, 把一篇目标笔记/一条目标人群转成
    「该留什么公开留言 + 对方回应后私信往哪引」的可执行动作。
  * 命中判断: 一篇笔记的标题/正文/标签是否命中某个 target 的 note_match。
  * 留言文案生成: 优先走回复策略+LLM(贴 target 的 comment_style/hook),
    无 LLM 用规则兜底(hook 直接按 target 给, 去敏感词)。

★ 与回复策略系统的关系:
  回复策略(reply_strategy.yaml)   : 对方【已私信/已评论】后, 我们怎么回。
  主动留言(proactive_targets.yaml): 对方还【不认识我们】, 我们怎么主动去撩。
  两者共用红线: 公开区(评论区)只种草/提问, 绝不导流/卖课/留联系方式。
  引流动作一律在【对方进了私信/有回应】之后才做。

用法(纯函数, 可在 console/server 或独立脚本里调用):
  from proactive_plan import load_targets, match_target, plan_comment
  ts = load_targets()
  t  = match_target(ts, title="带团太累了", body="研学线路怎么排")
  cm = plan_comment(t, note_title="带团太累了")
    → {"comment": "...", "target": "研学/带团同行", "dm_direction": "..."}
"""
from __future__ import annotations

import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
YAML_FP = os.path.join(HERE, "proactive_targets.yaml")

# 公开区留言红线(仅供阅读参考; 实际判红统一走 reply_compose._pass_red(channel="public"))
_PUBLIC_RED = (
    # —— 联系方式(明文直接违规) ——
    "微信", "wx", "vx", "VX", "薇", "微心", "薇信", "威信", "绿泡泡",
    "企鹅", "添加", "加V", "加v", "+v", "＋v", "➕", "号码", "手机号",
    "手机", "电话", "q号", "qq", "扣扣", "二维码", "扫码", "扫我",
    # —— 公开直引(评论区"私信我/加我"优先监控, 引导往私信也要克制) ——
    "私信我", "私聊我", "咨询我", "找我聊", "私我", "主页",
    # —— 广告/卖课/报价(公开区严禁) ——
    "报名", "课程", "课程价", "报名费", "1499", "4980", "20-60", "元/条",
    "包接单", "包涨薪", "包升学", "保证", "稳赚", "躺赚", "绝对", "最便宜",
    # —— 绝对化承诺(平台明确打击的营销极限词) ——
    "全网最", "行业第一", "全国第一", "没有之一", "yyds", "无敌",
    "最划算", "赔本", "名额有限", "最后X名", "快抢", "速来",
)


def load_targets():
    """读 proactive_targets.yaml。异常 → 返回空结构(不炸)。"""
    try:
        import yaml
        with open(YAML_FP, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        return d
    except Exception:
        return {"targets": {}, "global": {}}


def enabled_targets(cfg):
    """返回已启用的 target 列表 [ (key, tgt) ]."""
    d = (cfg or {}).get("targets", {}) or {}
    return [(k, t) for k, t in d.items() if t.get("enabled", True)]


def _blocked(tgt, text):
    """判断某 tgt 因 not_match 排除语义而被跳过(如"机构/旅行社/师资" → 归 B 端而非个人同行)。"""
    nm = (tgt.get("not_match") or "").strip()
    return bool(nm) and bool(re.search(nm, text))


def match_target(cfg, title="", body="", tags="", key="",
                 prefer_track=None, enabled_only=True):
    """判断一篇笔记命中哪个 target。返回 (key, tgt) 或 (None, None)。
    ★ 逻辑优化(自查): 原来"顺序优先"会让宽泛 target(如 qianzai_xueyuan 靠 keywords)
      抢走细分 target(如 jiedan_xinshou 用 note_match 精确)的人, 造成人群串扰。
      现改为【命中得分 + 赛道优先】: 每个 target 按匹配强度打分,
      → note_match 精确命中(高) > keywords 子串(中) > non_match(低),
      → 同等分下, prefer_track(当前账号赛道)的 target 优先,
      → not_match 命中直接排除(保持原红线)。
    返回得分最高的 (key, tgt)。"""
    d = (cfg or {}).get("targets", {}) or {}
    if key and key in d:                       # 显式指定 target: 直接返回
        t = d[key]
        if t.get("enabled", True) and not _blocked(t, hay_of(title, body, tags)):
            return (key, t)
    hay = hay_of(title, body, tags)
    best_key, best_tgt, best_score = None, None, -1
    for k, t in enabled_targets(cfg):
        if not t.get("enabled", True):
            continue
        if _blocked(t, hay):                   # not_match 命中 → 排除
            continue
        nm = t.get("note_match") or ""
        kws = t.get("keywords") or []
        score = 0
        if nm and isinstance(nm, str) and nm:
            # note_match 命中: 若关键词也命中则更强(+2), 只正则为 +1
            if re.search(nm, hay):
                score += 3 if any((isinstance(kw, str) and kw and kw in hay) for kw in kws) else 2
        kw_hits = [kw for kw in kws if isinstance(kw, str) and kw and kw in hay]
        if kw_hits:
            # 命中数越多 + 越接近"细分词"(2字以上)越优先
            frag = sum(len(kw) for kw in kw_hits)
            score += 1 + min(len(kw_hits), 4) + (frag // 6)
        # 当前账号赛道优先(同分时), +1 不喧宾夺主
        if prefer_track and t.get("track") == prefer_track:
            score += 1
        if score > 0 and score > best_score:
            best_score, best_key, best_tgt = score, k, t
    return (best_key, best_tgt)


def hay_of(title="", body="", tags=""):
    return f"{title}\n{body}\n{tags}"


def _pass_red(text, channel="public"):
    """公开区留言红线(默认 public=最严)。主动留言是【公开评论】, 必须走最严通道:
    禁 导流/卖课/报价/联系方式/谐音变体/绝对化。复用 reply_compose 的通道红线。"""
    try:
        from reply_compose import _pass_red as _r
        return _r(text, channel=channel)
    except Exception:
        # 独立兜底红清单(与 reply_compose._PUBLIC_RED 保持一致的最严口径)
        low = (text or "").lower()
        return not any(b.lower() in low for b in (
            "微信", "vx", "薇", "绿泡泡", "威信", "加V", "号码", "扫码", "二维码",
            "手机号", "电话", "私信我", "私聊我", "私我", "报名", "课程", "1499",
            "4980", "20-60", "元/条", "包接单", "包涨薪", "包升学", "保证", "稳赚",
            "躺赚", "绝对", "全网最", "行业第一", "全国第一", "没有之一", "最划算",
            "名额", "快抢", "速来",
        ))


def _strip_think(text):
    """剥 <think>...</think>(与 reply_compose 一致的坑: 推理块里模型会提到禁词)。"""
    if not text:
        return text
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in t:
        t = re.sub(r"<think>.*", "", t, flags=re.S)
    return t.strip().strip("\"'").strip()


def _llm_comment(tgt, note_title, note_body):
    """尝试用回复策略同款 LLM 生成公开留言。遍历 provider 链(deepseek→glm→minimax),
    逐个试, 任一成功即用; 全失败返回 None。"""
    try:
        sys.path.insert(0, HERE)
        from reply_compose import iter_llm_chain
        import urllib.request, json
        # ★ 策略增强: 把 target 的多角度策略(comment_angles)喂给 LLM, 让它按笔记特征挑最贴角度,
        #   而不是只给单条 hook 模板。真正"输出完整策略", 不套固定模板。
        angles = tgt.get("comment_angles") or []
        angle_lines = "\n".join(
            f"  - 切入点[{i+1}] {a.get('label','')} @{a.get('when','')}: {a.get('text','')[:90]}"
            for i, a in enumerate(angles)) or "  (无, 用通用真诚共情)"
        sys_prompt = (
            "你是小红书评论区内容创作者。根据目标人群和笔记内容, 写一条【公开评论】。\n"
            "要求:\n"
            "1. 像真人真诚评论, 不是广告/不是客服腔。先接住笔记里的真实点, 再给一个可落地的思路。\n"
            "2. 结尾用一个[好问题]引对方回应(让他愿意回复你)。\n"
            "3. 绝不在公开区出现: 微信/手机号/扫码/报名/课程/价格/绝对化词/导流词。引流只留在私信。\n"
            "4. 只输出那条评论本身, 不要解释、不要引号。\n"
            f"5. 风格: {tgt.get('comment_style','')}\n"
            "6. 从下面的【可用切入点】里, 选一个最贴合这篇笔记的角度来写(不要照抄, 调整成这篇笔记的语气):\n"
            f"{angle_lines}"
        )
        user_prompt = (
            f"【目标人群】{tgt.get('name','')}\n"
            f"【笔记标题】{note_title}\n"
            f"【笔记内容】{(note_body or '')[:400]}\n"
            f"请按上面最贴合的角度, 写那条公开评论。"
        )
        payload = {"model": None,  # 下面按 provider 填
                   "messages": [{"role": "system", "content": sys_prompt},
                                {"role": "user", "content": user_prompt}],
                   "max_completion_tokens": 1200}
        # 逐个 provider 试
        for prov, key, base, model in iter_llm_chain():
            if not key or not base:
                continue
            payload["model"] = model
            req = urllib.request.Request(
                f"{base}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                txt = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                txt = _strip_think((txt or "").strip().strip("\"'"))
                if txt and _pass_red(txt):
                    return txt
            except Exception:
                continue  # 该 provider 失败, 试下一个
        return None
    except Exception:
        return None


def _pick_angle(tgt, note_title="", note_body=""):
    """★ 策略增强: 根据笔记特征, 从 target 的 comment_angles(多个评论角度)里
    选出最贴切的一个角度, 用它的 text 作为留言。这是"不套固定模板"的核心:
    不再是死用一条 hook, 而是按笔记的 when 条件动态挑最合适的切入点。
    返回 (label, text) 或 (None, None)。"""
    angles = tgt.get("comment_angles") or []
    if not angles:
        return (None, None)
    hay = f"{note_title}\n{note_body}"
    # 把 when 条件切成语义信号词(去除非语义的"对方/写/说/问"等)
    _stop = ("对方", "写", "说", "问", "提到", "当", "是", "有", "在")
    # 用"打分法": 每个角度对笔记文本算匹配分, 取最高; 而非第一条命中即停
    best_score, best_angle = 0, None
    for a in angles:
        when = (a.get("when") or "").strip()
        if not when:
            continue
        signals = [s for s in re.split(r"[、,，/ +]+", when) if s and s not in _stop]
        score = 0
        for sig in signals:
            if not sig:
                continue
            # 双向包含: 笔记含信号 或 信号含笔记词根
            if sig in hay:
                score += 2
            # 信号词比笔记词短且是子串(如 信号"累" ⊂ 笔记"带团累")
            elif len(sig) >= 2 and any(sig in hw for hw in re.split(r"[。！？?!，,\s]+", hay) if hw):
                score += 1
        # 语义加成: 关键情绪/意图词直接命中 → 高加权
        for kw in ("失眠", "累", "难", "怕", "割", "焦虑", "方法", "流程",
                   "什么", "怎么", "求", "带团", "讲解", "副业", "赚钱", "接单"):
            if kw in hay:
                if any(kw in (s or "") for s in signals):
                    score += 3
        if score > best_score:
            best_score, best_angle = score, a
    if best_angle is not None:
        return (best_angle.get("label", ""), best_angle.get("text", ""))
    # 兜底: 取第一条角度
    return (angles[0].get("label", ""), angles[0].get("text", ""))


def _rule_comment(tgt, note_title="", note_body=""):
    """无 LLM: 优先用"按笔记特征选中的角度 text"; 没有 angles 才退到 hook 抓句。"""
    # ★ 优先: comment_angles 动态选角度(策略增强核心)
    label, text = _pick_angle(tgt, note_title, note_body)
    if text and _pass_red(text):
        return text
    # 兜底: 从 hook 里抓真正的评论句
    hook = tgt.get("hook") or ""
    # 1) 优先取双引号内的内容(示例句最真实)
    for q in ('"', "'", "“", "‘"):
        m = re.search(r"%s(.*?)%s" % (q, q), hook, flags=re.S)
        if m:
            cand = m.group(1).strip()
            if cand and _pass_red(cand):
                return cand
    # 2) 取 '例:' 之后的第一句
    for marker in ("例:", "例子:", "示例:", "例："):
        if marker in hook:
            tail = hook.split(marker, 1)[1]
            seg = tail.split("\n")[0].strip().strip(('"' "'")).strip()
            if seg and _pass_red(seg):
                return seg
    # 3) 兜底: 取 hook 里最长的行
    best = ""
    for line in hook.split("\n"):
        line = line.strip()
        if len(line) > len(best) and _pass_red(line):
            best = line
    return best


def plan_comment(cfg, tgt, note_title="", note_body="", prefer="llm"):
    """给一篇命中 target 的笔记生成公开留言 + 私信承接方向。
    返回 {"comment", "engine", "target", "dm_direction", "hit"}."""
    if tgt is None:
        return {"comment": "", "engine": "none", "target": "",
                "dm_direction": "", "hit": False}
    comment = ""
    engine = "rule"
    if prefer == "llm":
        comment = _llm_comment(tgt, note_title, note_body) or ""
        if comment:
            engine = "llm"
    if not comment:
        comment = _rule_comment(tgt, note_title, note_body)
        engine = "rule"
    return {"comment": comment, "engine": engine,
            "target": tgt.get("name", ""),
            "dm_direction": tgt.get("dm_direction", ""),
            "hit": True}


def dummy(cfg, prefer="llm"):
    """自检: 模拟一篇笔记(不存在), 验证 match_target + plan_comment 链路。"""
    # 用一段研学场景文本, 且不含导流词
    demo = "带团六年了, 每年都累, 但最近在想用AI做线路和内容会不会省点事"
    key, tgt = match_target(cfg, title="带团六年", body=demo)
    print(f"命中目标: key={key!r} name={tgt.get('name') if tgt else None}")
    if tgt:
        pl = plan_comment(cfg, tgt, "带团六年", demo, prefer=prefer)
        print(f"  [engine={pl['engine']}] comment={pl['comment']}")
        print(f"  私信方向: {pl['dm_direction']}")
        print(f"  通过公开区红线: {_pass_red(pl['comment'])}")
    return key, tgt


if __name__ == "__main__":
    cfg = load_targets()
    print(f"启用目标: {[k for k,_ in enabled_targets(cfg)]}")
    print()
    dummy(cfg, prefer="llm")
