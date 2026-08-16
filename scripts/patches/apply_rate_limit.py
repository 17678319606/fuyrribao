#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时给 fuyr 三个脚本就地注入 AIRateLimiter（统一 AI 限速器）。

设计目的：
  - 仓库源码（scripts/common.py 等）保持「无限速器」基线，本脚本在 CI 运行时
    对其就地打补丁，规避大文件全量传输限制；幂等（已注入则跳过）。
  - 修复旧方案「非线程安全 _gemini_last_call + 4s 最小间隔」的三类根因：
      1) 仅最小间隔、踩在 15 RPM 边缘零余量 → RPM 滑窗（默认 10，留余量）；
      2) 无每日预算 guard、多次触发打光 1500 RPD → 每日预算（默认 800）；
      3) 无并发闸 → 全局并发闸（默认 2）。
  「慢点没关系」：宁可均匀摊开也不撞墙把免费额度打光。
用法：python apply_rate_limit.py   （cwd = 仓库根）
"""
import os
import py_compile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根（脚本在 scripts/patches/）
if not os.path.exists(os.path.join(ROOT, "scripts", "common.py")):
    # 兼容直接放在仓库根或被 cwd 调用的情况
    ROOT = os.getcwd()


def _p(name):
    return os.path.join(ROOT, "scripts", name)


def patch_common():
    path = _p("common.py")
    s = open(path, encoding="utf-8").read()
    if "class AIRateLimiter" in s:
        print("[common] 已注入，跳过")
        return
    block = '''import threading
import time

# ─────────────────────────────────────────────────────────────────────
# 统一 AI 限速器（Gemini 免费层配额保护）
# ─────────────────────────────────────────────────────────────────────
AI_RATE_STATE_FILE = os.path.join(STATE_DIR, "ai_rate_state.json")

def is_gemini_host(url):
    """判断端点是否为 Google Gemini 原生 API（决定是否计入免费层配额预算）。"""
    low = (url or "").lower()
    return "generativelanguage.googleapis.com" in low and "/openai/" not in low


class AIRateLimiter:
    def __init__(self):
        self.rpm = float(os.environ.get("GEMINI_RPM", "10"))
        self.rpd = float(os.environ.get("GEMINI_RPD", "800"))
        self.max_concurrency = int(os.environ.get("GEMINI_MAX_CONCURRENCY", "1"))  # 用户明确要求串行：并发度=1
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(self.max_concurrency)
        self._window = []
        self._day = None
        self._used = 0
        self._loaded = False
        self._load()

    def _load(self):
        try:
            d = load_json(AI_RATE_STATE_FILE, {})
            self._day = d.get("day", date_str())
            self._used = float(d.get("used", 0))
        except Exception:
            self._day = date_str()
            self._used = 0
        self._rollover_day()
        self._loaded = True

    def _save(self):
        try:
            obj = {"day": self._day, "used": self._used,
                   "updated": datetime.datetime.now().isoformat()}
            save_json(AI_RATE_STATE_FILE, obj)
        except Exception:
            pass

    def _rollover_day(self):
        today = date_str()
        if self._day != today:
            self._day = today
            self._used = 0
            self._window = []
            self._save()

    def _admit_rpm(self):
        min_gap = 60.0 / max(1.0, self.rpm)
        now = time.time()
        with self._lock:
            self._window = [t for t in self._window if now - t < 60.0]
            if self._window:
                elapsed = now - self._window[0]
                need = min_gap * len(self._window)
                if elapsed < need:
                    return need - elapsed + 0.2
        return 0.0

    def throttle(self, is_gemini=True):
        with self._sem:
            self._rollover_day()
            if is_gemini:
                if self._used >= self.rpd:
                    raise RuntimeError(
                        "Gemini 免费层每日预算已用尽（%d/%d），中止本次 AI 调用避免撞墙；"
                        "请明日再跑或升级付费层。" % (int(self._used), int(self.rpd)))
                wait = self._admit_rpm()
                if wait > 0:
                    logging.getLogger("fuyr.ai_limiter").warning(
                        "AI 限速（RPM 滑窗）：补眠 %.1fs 以避免撞 Gemini 15 RPM 墙", wait)
                    time.sleep(wait)
                with self._lock:
                    self._window.append(time.time())
                    self._used += 1
                # 每次调用都持久化（文件极小 ~250B）：跨进程/跨运行预算可追、快照可读真实计数。
                self._save()

    def snapshot(self):
        self._rollover_day()
        # 优先读持久化文件（跨进程/跨运行真实计数），内存值兜底
        used = self._used
        try:
            d = load_json(AI_RATE_STATE_FILE, {})
            if d.get("day") == self._day:
                used = float(d.get("used", used))
        except Exception:
            pass
        return {"day": self._day, "used": int(used), "rpm": self.rpm, "rpd": self.rpd}


ai_limiter = AIRateLimiter()
'''
    anchor = "    return min(1.0, 0.4 + 0.12 * hits)\n"
    assert anchor in s, "common anchor not found"
    s = s.replace(anchor, anchor + "\n" + block, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("[common] AIRateLimiter 注入 OK")


def patch_report():
    path = _p("generate_report.py")
    s = open(path, encoding="utf-8").read()
    old_def = '''# —— Gemini 免费层限速器 ——
# Google AI Studio 免费层限制 15 RPM（每分钟 15 次请求）。
# screen_signals() 分批筛选会在几秒内连续打 ~10 次 AI 调用 → 直接撞 429。
# 本模块级变量记录上次 Gemini 原生调用时间，每次调用前自动补眠至最小间隔，
# 所有调用方（筛选循环 / 主生成 / daily_summary）统一受控，无需逐处改。
_gemini_last_call = 0.0   # 上次 _call_gemini_native 发起请求的 time.time()
GEMINI_RATE_INTERVAL = float(os.environ.get("GEMINI_RATE_INTERVAL", "4.0"))  # 默认 4s → ≤15 RPM
'''
    new_def = '''# —— AI 限速器（统一）——
# 旧方案（非线程安全的 _gemini_last_call + 4s 最小间隔）已废弃；
# 改用 common.ai_limiter（RPM 滑窗 + 每日预算 + 并发闸），见 common.py。
'''
    assert old_def in s, "report old_def 锚点缺失（源码可能已变更）"
    s = s.replace(old_def, new_def, 1)
    old_in = '''    # 免费层限速：确保两次 Gemini 调用间隔 ≥ GEMINI_RATE_INTERVAL（默认 4s），
    # 避免分批筛选等循环密集调用撞 15 RPM 墙导致 429。
    global _gemini_last_call
    elapsed = time.time() - _gemini_last_call
    if elapsed < GEMINI_RATE_INTERVAL and _gemini_last_call > 0:
        wait = GEMINI_RATE_INTERVAL - elapsed
        LOG.info("Gemini 限速：距上次调用 %.1fs，补眠 %.1fs（间隔=%.1fs）",
                 elapsed, wait, GEMINI_RATE_INTERVAL)
        time.sleep(wait)
    _gemini_last_call = time.time()
'''
    new_in = '''    # 统一限速器：免费层 RPM 滑窗 + 每日预算，避免密集调用撞 429 / 打光配额。
    C.ai_limiter.throttle(is_gemini=True)
'''
    assert old_in in s, "report old_in 锚点缺失（源码可能已变更）"
    s = s.replace(old_in, new_in, 1)
    old_openai = '''        url = base_url.rstrip("/") + "/chat/completions"
        # 解析目标 host：仅国内网关等易抖动 host 才用 DoH 兜底钉 IP；'''
    new_openai = '''        url = base_url.rstrip("/") + "/chat/completions"
        C.ai_limiter.throttle(is_gemini=C.is_gemini_host(base_url))
        # 解析目标 host：仅国内网关等易抖动 host 才用 DoH 兜底钉 IP；'''
    assert old_openai in s, "report old_openai 锚点缺失（源码可能已变更）"
    s = s.replace(old_openai, new_openai, 1)
    old_nb = '''            try:
                r2 = requests.post(url, headers=headers, json=nb_payload,
                                   proxies=None, timeout=timeout)'''
    new_nb = '''            try:
                C.ai_limiter.throttle(is_gemini=C.is_gemini_host(base_url))
                r2 = requests.post(url, headers=headers, json=nb_payload,
                                   proxies=None, timeout=timeout)'''
    assert old_nb in s, "report old_nb 锚点缺失（源码可能已变更）"
    s = s.replace(old_nb, new_nb, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("[report] 注入 OK")


def patch_roundup():
    path = _p("generate_roundup.py")
    s = open(path, encoding="utf-8").read()
    old = '''            try:
                r = requests.post(url, headers=auth, json=body, timeout=150)'''
    assert old in s, "roundup 锚点缺失（源码可能已变更）"
    new = '''            try:
                C.ai_limiter.throttle(is_gemini=C.is_gemini_host(base))
                r = requests.post(url, headers=auth, json=body, timeout=150)'''
    s = s.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("[roundup] 注入 OK")


def patch_fetch():
    """fetch_signals.py：单篇正文截断保底（用户建议：数据减肥，省 token 提速，简报只需核心大意）。"""
    path = _p("fetch_signals.py")
    s = open(path, encoding="utf-8").read()
    if "SIGNAL_MAX_CHARS" in s:
        print("[fetch] 已注入，跳过")
        return
    old = '    return text.strip()\n'
    assert s.count(old) == 1, "fetch anchor not unique/found"
    new = ('    # 数据减肥（用户建议）：单篇正文截断保底，省 token 提速，简报只需核心大意。\n'
           '    if len(text) > 1000:\n'
           '        text = text[:1000].rstrip() + "..."\n'
           '    return text.strip()\n')
    s = s.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("[fetch] 注入 OK（正文截断≤1000字）")


def verify():
    ok = True
    for f in ("common", "generate_report", "generate_roundup", "fetch_signals"):
        try:
            py_compile.compile(_p(f + ".py"), doraise=True)
        except Exception as e:
            ok = False
            print("[compile FAIL] %s: %s" % (f, e))
    print("COMPILE_ALL_OK" if ok else "COMPILE_HAS_ERRORS")
    return ok


if __name__ == "__main__":
    patch_common()
    patch_report()
    patch_roundup()
    patch_fetch()
    verify()
    print("APPLY_RATE_LIMIT_DONE")
