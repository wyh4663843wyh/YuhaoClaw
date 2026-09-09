#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书 评论区采集器(只读 · 不发送)         2026-09-04 真机探索定型

职责:进入「消息 → 收到的评论和@」聚合页 + 逐条点进对应笔记详情,读取
      自己笔记上的真实评论(评论人/正文/时间/是否作者回复/笔记来源),结构化落 CSV。
      与 collect_inbox.py(私信)互补:评论区是更前置的获客触点。

红线:只读不发送;不点『回复』钮、不评不赞;进入笔记详情仅停留读取后即退。
真相(2026-09-04 真机 红米K40 alioth 实测):
  * 消息总览顶部三入口: 赞和收藏 / 新增关注 / 评论和@  —— 评论走『评论和@』
  * 『收到的评论和@』列表每条 = [评论人昵称][动作:评论了你的笔记/回复了你的评论][时间][正文][回复钮/赞钮]
  * 点某条『评论了你的笔记』行 → 进入该笔记 NoteDetailActivity,顶部=作者+[评论 N/赞和收藏 N],
    整页即该笔记真实评论区(含作者对每条评论的回复) → 笔记来源/评论区全量一次拿到
  * 反例:私信会话言玉/苹果来源卡此前已探(README §9);评论区天然带笔记上下文,归因更直接
  * 风险:平台反诈/『评论已隐藏』会在详情里出现(经历换未来那条是被隐藏的高意向),采到原样保留由真人判

用法(设备已连,如红米K40):
  python collect_comments.py --once      # 采一轮当前可见评论 + 进详情归因(默认)
  python collect_comments.py --aggr      # 只采『收到的评论和@』聚合列表,不强进详情

依赖:同 collect_inbox(pip 装好的隔离 env) uiautomator2;手机开 USB 调试。
"""
from __future__ import annotations

import argparse, csv, os, re, sys, time, datetime
PY = sys.executable

HERE   = os.path.dirname(os.path.abspath(__file__))
ROOT   = os.path.dirname(HERE)
OUT_DIR= os.path.join(ROOT, "out")
CSV_C  = os.path.join(OUT_DIR, "leads_comments.csv")   # 评论区落库
PKG    = "com.xingin.xhs"

# 我方已发布笔记标题片段 → (规范名, src_type)  —— 与 collect_inbox.KNOWN_NOTE_TITLES 同源
KNOWN_NOTE_TITLES = {
    "你的笔记A": ("你的笔记A", "opc_recruit"),
    "你的笔记B": ("你的笔记B", "xueyan_ai"),
    "你的笔记C": ("你的笔记C", "tool_public"),
}
_KNOWN_MATCH_ORDER = sorted(KNOWN_NOTE_TITLES, key=len, reverse=True)

# 动作类型文本(出现在评论聚合页 每条的『动作』位)
ACT_COMMENT  = "评论了你的笔记"
ACT_REPLY    = "回复了你的评论"
ACT_LIKE_COL = "赞和收藏"
ACT_FOLLOW   = "新增关注"

# 顶部『评论和@』入口近似坐标(IndexActivityV2 消息总览顶部条)——真机测得 x≈797-944,y≈411-456
_COMMENT_TAB_X_RANGE = (300, 1080)   # 兜底文本锚优先,坐标仅参考


def connect():
    import uiautomator2 as u2
    serial = os.environ.get("XHS_DEVICE", "")
    return u2.connect(serial) if serial else u2.connect()


def parse(xml):
    out = []
    for m in re.finditer(r'<node[^>]*?/>', xml):
        t = m.group(0)
        def g(k):
            mm = re.search(k + r'="([^"]*)"', t)
            return mm.group(1) if mm else ''
        out.append({'text': g('text'), 'desc': g('content-desc'),
                    'b': g('bounds'), 'click': g('clickable') == 'true',
                    'cla': g('class')})
    return out


def bbox(b):
    m = re.findall(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', b)
    return tuple(map(int, m[0])) if m else None


def getf(n): return (n['text'] or n['desc'] or '').strip()


def _act_role(action_text):
    """动作文本归属:点进此条是否入笔记评论详情。只『评论/回复』类才点,赞藏/关注不点。"""
    if ACT_COMMENT in action_text or ACT_REPLY in action_text:
        return "comment"
    if ACT_LIKE_COL in action_text or ACT_FOLLOW in action_text:
        return "meta"
    return "other"


def _norm_note(title_frag):
    for k in _KNOWN_MATCH_ORDER:
        if k.lower() in (title_frag or "").lower():
            return KNOWN_NOTE_TITLES[k]
    return (None, "")


def goto_comment_tab(d):
    """从任意层回消息总览并按下『评论和@』;每次独立重进,结束不留状态。
    返回 (ok, trees);trees=滚过的收到的评论列表树。

    真机导航固定路径(2026-09-04 实测 红米K40):
      冷起 stop/start → 发现页 Index(tab条:首页/市集/消息/我)
      → 底部点『消息』Tab(xhs tab 文本 click=False,须点其坐标才切屏)
      → 消息总览顶部三入口『赞和收藏/新增关注/评论和@』
      → 点『评论和@』→ 进入『收到的评论和@』聚合页。
    注:xhs 底部 tab 与顶部入口的 TextView click=False,只能按坐标点。
    """
    trees = []
    # 1) 强制回发现页(即便已在消息中心也 stop 后重开,保证起点一致)
    try:
        d.app_stop(PKG); time.sleep(0.6)
    except Exception:
        pass
    d.app_start(PKG); time.sleep(1.9)
    # 2) 点底部『消息』Tab(约 y=2290);文本找到就用它的坐标,找不到用固定 y 扫 x
    def bottom_tab_x(label):
        xml = d.dump_hierarchy(); ns = parse(xml)
        for n in ns:
            bb = bbox(n['b'])
            if getf(n) == label and bb and bb[1] > 2200:
                return (bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2
        # 兜底:消息 Tab 通常 x≈755
        return (755, 2295)
    for _ in range(4):
        xml = d.dump_hierarchy(); ns = parse(xml)
        has_tabs = any(getf(n) in ("首页", "市集", "消息", "我")
                       and bbox(n['b']) and bbox(n['b'])[1] > 2200 for n in ns)
        if has_tabs:
            # 已在带底 tab 的页;点消息
            cx, cy = bottom_tab_x("消息")
            d.click(cx, cy); time.sleep(1.6)
            break
        xml = d.dump_hierarchy(); ns = parse(xml)
        # 若已停在消息总览(顶部三入口)则直接进第2步
        if any('评论和@' in getf(n) for n in ns):
            break
        d.press('back'); time.sleep(0.8)
    # 3) 在消息总览顶部按文本『评论和@』点(坐标兜底 x≈797-944,y≈411-456)
    for _ in range(5):
        xml = d.dump_hierarchy()
        trees.append(xml)
        ns = parse(xml)
        c = next((n for n in ns if '评论和@' in getf(n)), None)
        if c:
            bb = bbox(c['b'])
            if bb:
                d.click((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)
                time.sleep(1.8)
                return True, trees   # 已进入『收到的评论和@』页
        # 兜底:顶部入口此屏宽测得 x≈797-944,y≈411-456
        d.click(870, 433)
        time.sleep(1.6)
        xml2 = d.dump_hierarchy()
        trees.append(xml2)
        if any('收到的评论和@' in getf(n) or '收到的赞和收藏' in getf(n)
               for n in parse(xml2)):
            return True, trees
    return False, trees


def collect_list(d, max_scroll=8):
    """收『收到的评论和@』聚合页当前可读的全部评论块(不点进)。
    返回 list of dict: user/action_when/ts/text/has_reply/has_like。"""
    seen_sign = ""
    items = []
    for _ in range(max_scroll):
        xml = d.dump_hierarchy()
        ns = parse(xml)
        # 这条动作与前一条的正文结构:行= (user, click) ... (动作,时间) ... (正文...)
        # 用『动作文本』作为每条的锚,回溯其上方最近昵称 + 下方最近的正文
        texts = []
        for n in ns:
            f = getf(n); bb = bbox(n['b'])
            if not bb or not f or bb[1] < 90: continue
            texts.append((bb[1], bb[3], f, n['click'], bb))
        texts.sort()
        # 分割块:动作含 ACT_COMMENT/ACT_REPLY → user 块(i-几行里出现可点昵称)
        for i, (y1, y2, f, cl, bb) in enumerate(texts):
            r = _act_role(f)
            if r != "comment":
                continue
            # 找块起始:动作行上方最近的『可点昵称/纯文本』作为 user
            user = ""
            for j in range(i - 1, -1, -1):
                uy = texts[j][1]
                if y1 - uy > 220 or texts[j][3]:   # 上方越界 / 昵称可点 → 取它
                    if texts[j][3] and texts[j][0] < y1:
                        user = texts[j][2]; break
                if j == 0:
                    user = texts[0][2]
            if not user:
                continue
            # ts:同一行通常含日期
            ts = f
            body = ""
            # 动作行下一行通常是正文
            for j in range(i + 1, len(texts)):
                if texts[j][0] - y2 <= 130 and texts[j][2] not in ("回复", "赞"):
                    if texts[j][0] - y2 >= -5:
                        body = texts[j][2]; break
            items.append({"user": user, "action": f, "body": body, "ts": ts})
        # 收敛/滚到底判断
        sig = ";".join(f"{it.get('user')}|{it.get('body', '')[:8]}" for it in items[-8:])
        if sig and sig == seen_sign:
            break
        seen_sign = sig
        d.swipe(540, 1750, 540, 550, duration=0.4); time.sleep(0.7)
    return items


def open_note_detail(d, center=(540, 1000)):
    """在当前『收到的评论和@』页,点某条评论行 → 进入对应笔记详情评论区。
    若已是 NoteDetail 直接返回(dump 树);否则点屏幕中部一条可点评论行。返回 None 或 xml"""
    xml = d.dump_hierarchy()
    act = (d.app_current().get('activity') or '')
    if 'NoteDetailActivity' in act:
        return xml
    ns = parse(xml)
    # 找一条『评论了你的笔记』的行里可点昵称,点开它
    for n in ns:
        f = getf(n); bb = bbox(n['b'])
        if not bb: continue
        if ACT_COMMENT in f and n['click']:
            d.click((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)
            time.sleep(2.2)
            xml = d.dump_hierarchy()
            if 'NoteDetailActivity' in (d.app_current().get('activity') or ''):
                return xml
            break
    return None


def parse_note_detail(xml, d):
    """从 NoteDetail 页取该笔记: 作者 / 评论统计 / 标题线索 / 评论区(真人可执行来源判定)。
    返回 {author, stat, comment_rows, src_note, src_type}"""
    ns = parse(xml)
    author = next((getf(n) for n in ns if getf(n) and '工作流' in getf(n)), "")
    stat = ""
    for n in ns:
        f = getf(n)
        if re.match(r'^(评论|赞和收藏)\s*\d+', f):
            stat += f + "  "
    if not stat:  # 顶到评论列表下方再找
        d.swipe(540, 400, 540, 1500, duration=0.3); time.sleep(0.5)
        xml = d.dump_hierarchy(); ns = parse(xml)
        for n in ns:
            f = getf(n)
            if re.match(r'^(评论|赞和收藏)\s*\d+', f):
                stat += f + "  "
    # 评论行:作者主评论区(非『说点什么』输入框);粗取带『作者』标识或昵称密集区正文
    rows = []
    for n in ns:
        f = getf(n); bb = bbox(n['b'])
        if not bb or not f or bb[1] < 150: continue
        if f in ("回复", "赞", "作者", "...", "说点什么", "展开 1 条回复", "评论已隐藏"):
            continue
        if bb[1] > 2350: continue     # 底输入/点赞条之外
        # 收集含中文内容的主体文本
        if len(f) >= 2 and re.search(r'[\u4e00-\u9fa5]', f) and not re.match(r'^[\d\-: ]+$', f):
            rows.append(f)
    src_note, src_type = "", ""
    # 笔记标题通常在作者下方首行外。此处以 author + 首条用户评论反查不出标题→在分级由真人/标题段补;
    # 但可用作者=你的小红书昵称这一事实标 src_type=未知仅按会话归因
    return {"author": author, "stat": stat.strip(),
            "comment_rows": rows[:40], "src_note": src_note, "src_type": src_type}


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", default=True)
    ap.add_argument("--aggr", action="store_true", help="只采聚合列表不进详情(默认,最稳)")
    ap.add_argument("--deep", action="store_true",
                    help="仅对命中高意向关键词的评论点进笔记详情做来源归因(cap --max-open 条)")
    ap.add_argument("--max-open", type=int, default=4, help="deep 模式最多进详情条数")
    a = ap.parse_args()
    d = connect()
    ok, _ = goto_comment_tab(d)
    if not ok:
        print("[warn] 未能按『评论和@』入口进入")
        return
    print("[ok] 已进入『收到的评论和@』")
    items = collect_list(d, max_scroll=8)
    print(f"[已采] 聚合列表 {len(items)} 条")
    # 若已存在先前 csv 则先读已有 user+body 做去重,避免重复追加
    have = set()
    if os.path.exists(CSV_C):
        try:
            with open(CSV_C, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    have.add((r.get("user"), r.get("body")))
        except Exception:
            have = set()
    opened = 0
    for it in items:
        if (it["user"], it["body"]) in have:
            continue                      # 已落库过,跳过
        rec = {"ts": now(), "user": it["user"], "action": it["action"],
               "body": it["body"], "src_note": "", "src_type": "",
               "stat": "", "note_cmts": []}
        # deep:仅 '评论了' 且 body 命中的高意向词才进详情拿来源
        if a.deep and opened < a.max_open and it["action"].startswith("评论了") \
           and re.search(r'(咋|怎么|具体|做法|可以做|接单|多少钱|焦虑|导游|研学|报名)', it["body"]):
            info = parcel_note_detail_open(d)
            if info:
                rec["src_note"], rec["src_type"] = info["src_note"], info["src_type"]
                rec["stat"] = info["stat"]
                rec["note_cmts"] = info["comment_rows"]
            opened += 1
        append_one(rec)                    # 即时逐条写,崩溃不丢
        have.add((it["user"], it["body"]))
    print(f"[done] 评论区采集 {len(items) - len(have) + len(have)} / 新增落库 → {CSV_C}")
    # ⭐ 自动触发 话术库自学习闭环(2026-09-06 宇豪拍板"自动触发")
    # 评论也是高价值获客触点(导流技巧/业务咨询同样值得学习), 采集完自动反哺。子进程隔离。
    _auto_learn()


def _auto_learn():
    """评论区采集完成后自动反哺话术库(与 collect_inbox 同款, 零人工)。

    开子进程跑 classify/collect_learn_samples.py(导流 lead_angle 优先 + 常规咨询)
    → reply_self_learn 三路择优 → 星级 → 回灌叠加层。
    失败静默降级, 不阻断评论区采集主流程。
    """
    import subprocess, sys as _sys
    learn_py = os.path.join(ROOT, "classify", "collect_learn_samples.py")
    if not os.path.exists(learn_py):
        print("[learn] 跳过(collect_learn_samples.py 不存在)")
        return
    try:
        r = subprocess.run([_sys.executable, learn_py, "--no-llm"],
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()[-3000:]
        print(f"[learn] 自动话术库闭环输出:\n{out}")
        if r.returncode != 0 and (r.stderr or "").strip():
            print(f"[learn] 闭环 stderr: {r.stderr.strip()[-800:]}")
    except subprocess.TimeoutExpired:
        print("[learn] 话术库闭环超时(>120s), 已放弃, 不影响采集")
    except Exception as e:
        print(f"[learn] 自动闭环异常: {e}")


def append_one(r):
    """逐条即时写(utf-8-sig)。文件可能已被 --deep 多次调用持久化增补。"""
    if not os.path.exists(CSV_C):
        with open(CSV_C, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(["ts", "user", "action", "body", "note",
                                    "src_type", "stat", "note_cmts"])
    with open(CSV_C, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([r["ts"], r["user"], r["action"], r["body"],
                    r.get("src_note") or "", r.get("src_type") or "",
                    r.get("stat") or "", ";".join(r.get("note_cmts") or [])])


def parcel_note_detail_open(d):
    """deep 辅助:点当前聚合页某条可点评论→进详情取(作者/stat/评论区/来源)后返回。"""
    res = open_note_detail(d)
    act = (d.app_current().get('activity') or '')
    if res and 'NoteDetailActivity' in act:
        info = parse_note_detail(res, d)
        for _ in range(2):
            d.press('back'); time.sleep(0.8)
        time.sleep(0.5)
        goto_comment_tab(d)               # 回到聚合页(供下一条继续)
        return info
    # 未能进入详情则退回聚合页
    act2 = (d.app_current().get('activity') or '')
    if 'NoteDetailActivity' not in act2:
        goto_comment_tab(d)
    return None


if __name__ == "__main__":
    main()
