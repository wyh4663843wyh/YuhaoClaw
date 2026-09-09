#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多用户 · 多账号 · 多实例（升级1 多用户 SaaS 化）
--------------------------------------------------
在原有「单 out/ 目录」的基础上，引入【用户/账号分区】与【可切换的当前环境】：
  - 每个账号(设备)一个独立数据目录 out/<profile>/，天然做数据隔离。
  - 控制台可切换「当前环境(profile)」，读/写/执行都落在该 profile 的 out 下。
  - 登录态：轻量 token 会话（session_id → profile），默认本地单机用当前账号，
    无需强密码；多实例时每个实例一个 port + 一个 HOME 目录。

设计原则（半开源，示例可改）：
  - 不假定任何"用户表/数据库"——用文件目录当最简单的用户/账号隔离。
  - 真实的账号信息在 lanes.yaml accounts 里；profiles 只是"目录命名 + 会话映射"。
  - 保留向后兼容：不设 profile 时回退默认 out/（与原逻辑一致）。

用法：
  from profiles import ProfileEnv
  env = ProfileEnv(ROOT)          # 传入 interceptor/ 根
  env.set_profile("YOUR_DEVICE_SERIAL_1")   # 切到某账号
  out_dir = env.out()             # -> interceptor/out/@YOUR_DEVICE_SERIAL_1
  cls = env.cls()                 # 当前账号的 leads_classified.csv
  fol = env.fol()                 # 当前账号的 leads_followup.csv
  cmt = env.cmt()                 # 当前账号的 leads_comments.csv
  prolog = env.proactive_log()    # 当前账号的主动获客日志
"""
from __future__ import annotations
import os, json, time


class ProfileEnv:
    """多用户/多账号环境：解析当前 profile 的数据目录与文件路径。"""

    def __init__(self, root: str, profile: str = ""):
        self.root = root                          # interceptor/
        self.profile = profile or ""              # 当前选中(账号 id 或空=默认)
        self.lanes_fp = os.path.join(root, "console", "lanes.yaml")
        self._session = {}                        # session_id -> profile(登录态)

    # ---------------- 路径解析 ----------------
    def out(self) -> str:
        """当前 profile 的 out 目录；空 profile → 默认 out/。"""
        if not self.profile:
            return os.path.join(self.root, "out")
        # 用 @ 前缀区分账号分区, 避免与默认 out/ 混淆
        return os.path.join(self.root, "out", "@" + self._safe(self.profile))

    def cls(self) -> str:
        return os.path.join(self.out(), "leads_classified.csv")

    def fol(self) -> str:
        return os.path.join(self.out(), "leads_followup.csv")

    def cmt(self) -> str:
        return os.path.join(self.out(), "leads_comments.csv")

    def proactive_log(self) -> str:
        return os.path.join(self.out(), "proactive_send_log.json")

    def run_log(self) -> str:
        return os.path.join(self.out(), "proactive_run.log")

    def run_state(self) -> str:
        return os.path.join(self.out(), "proactive_run_state.json")

    def _safe(self, profile: str) -> str:
        """目录名安全化，防路径穿越。"""
        s = (profile or "").strip().replace("..", "_").replace("/", "_").replace("\\", "_")
        return s or "default"

    # ---------------- 账号列表(来自 lanes.yaml accounts) ----------------
    def accounts(self) -> dict:
        try:
            import yaml
            with open(self.lanes_fp, encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
            return d.get("accounts", {}) or {}
        except Exception:
            return {}

    def account_ids(self) -> list:
        return list(self.accounts().keys())

    def nick(self, profile: str = "") -> str:
        acc = self.accounts()
        pid = profile or self.profile
        m = acc.get(pid, {})
        return (m.get("nick") or pid) if pid else "默认设备"

    def lane(self, profile: str = "") -> str:
        acc = self.accounts()
        pid = profile or self.profile
        return (acc.get(pid, {}) or {}).get("lane", "")

    # ---------------- 登录态(轻量会话) ----------------
    def login(self, profile: str = "") -> str:
        """生成一个会话 token 并绑定到当前 profile。多实例时 key 放内存即可。"""
        sid = "s_" + str(int(time.time() * 1000)) + "_" + os.urandom(4).hex()
        self._session[sid] = profile or ""
        return sid

    def resolve_token(self, token: str) -> str:
        """token -> profile。无/失效 token 回退默认。"""
        if token and token in self._session:
            return self._session[token]
        return ""

    def profiles(self) -> list:
        """列出已有的 profile 分区目录名(不含默认 out/)。"""
        outr = os.path.join(self.root, "out")
        if not os.path.isdir(outr):
            return []
        return sorted(
            d[1:] for d in os.listdir(outr)
            if os.path.isdir(os.path.join(outr, d)) and d.startswith("@")
        )
