#!/usr/env python3
# -*- coding: utf-8 -*-
"""把四项深度优化「固化进仓库源码」（非运行时 patch），幂等、带断言防静默谎报。

对应 fuyrribao 用户建议：
  ① 增量缓存与本地持久化去重（崩溃续跑：合并后立即落盘 + 记录已生成信号）
  ② 分批串行 + 强制指数退避限速（AIRateLimiter 固化进 common + 组间随机 5-10s 抖动）
  ③ 后置数据清洗器（_extract_json 围栏剥离升级为任意语言/大小写兼容）
  ④ 级联异常捕获 + 局部容错发布（report 前置初始化防 NameError；report is None
     时仍落盘累积内容；增量检查点保证「天天见」）
用法：python apply_deep_opts.py   （cwd = 仓库根）
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.exists(os.path.join(ROOT, "scripts", "common.py")):
    ROOT = os.getcwd()


def _p(name):
    return os.path.join(ROOT, "scripts", name)


def _apply(path, old, new, label):
    s = open(path, encoding="utf-8").read()
    # 幂等优先：若 new 已存在（源码已固化），直接跳过，绝不重复注入或误报。
    if new.strip() in s:
        print("[%s] 已应用，跳过" % label)
        return
    assert old in s, "[%s] 锚点缺失（源码可能已变更，注入会失效）: %r" % (label, old[:60])
    s = s.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("[%s] 注入 OK" % label)


# ───────────────────────── common.py：AIRateLimiter 固化 ─────────────────────────
def patch_common():
    path = _p("common.py")
    s = open(path, encoding="utf-8").read()
    if "class AIRateLimiter" in s:
        print("[common] AIRateLimiter 已存在，跳过")
        return
    block = '''import threading
import time

# ─────────────────────────────────────────────────────────────────────
# 统一 AI 限速器（Gemini 免费层配额保护，已固化进源码）
# ─────────────────────────────────────────────────────────────────────
AI_RATE_STATE_FILE = os.path.join(STATE_DIR, "ai_rate_state.json")


def is_gemini_host(url):
    """判断端点是否为 Google Gemini 原生 API（决定是否计入免费层配额预算）。"""
    low = (url or "").lower()
    return "generativelanguage.googleapis.com" in low and "/openai/" not in low


class AIRateLimiter:
    def __init__(self):
        self.rpm = float(os.environ.get("GEMINI_RPM", "10"))      # 滑窗上限，留 33% 余量(<15)
        self.rpd = float(os.environ.get("GEMINI_RPD", "800"))     # 每日预算，留余量(<1500)
        self.max_concurrency = int(os.environ.get("GEMINI_MAX_CONCURRENCY", "1"))
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
                        "Gemini 免费层每日预算已用尽（%d/%d），中止本次原生调用避免撞墙；"
                        "请明日再跑或升级付费层。" % (int(self._used), int(self.rpd)))
                wait = self._admit_rpm()
                if wait > 0:
                    logging.getLogger("fuyr.ai_limiter").warning(
                        "AI 限速（RPM 滑窗）：补眠 %.1fs 以避免撞 Gemini 15 RPM 墙", wait)
                    time.sleep(wait)
                with self._lock:
                    self._window.append(time.time())
                    self._used += 1
                    self._save()

    def snapshot(self):
        self._rollover_day()
        return {"day": self._day, "used": int(self._used), "rpm": self.rpm, "rpd": self.rpd}


ai_limiter = AIRateLimiter()
'''
    anchor = "    return min(1.0, 0.4 + 0.12 * hits)\n"
    assert anchor in s, "common anchor not found"
    s = s.replace(anchor, anchor + "\n" + block, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("[common] AIRateLimiter 固化 OK")


# ───────────────────────── generate_report.py ─────────────────────────
def patch_report():
    path = _p("generate_report.py")
    s = open(path, encoding="utf-8").read()

    # E1: 删除旧弱限速器定义（4s 间隔踩 15 RPM 零余量），改指向 common.ai_limiter
    old_def = '''# —— Gemini 免费层限速器 ——
# Google AI Studio 免费层限制 15 RPM（每分钟 15 次请求）。
# screen_signals() 分批筛选会在几秒内连续打 ~10 次 AI 调用 → 直接撞 429。
# 本模块级变量记录上次 Gemini 原生调用时间，每次调用前自动补眠至最小间隔，
# 所有调用方（筛选循环 / 主生成 / daily_summary）统一受控，无需逐处改。
_gemini_last_call = 0.0   # 上次 _call_gemini_native 发起请求的 time.time()
GEMINI_RATE_INTERVAL = float(os.environ.get("GEMINI_RATE_INTERVAL", "4.0"))  # 默认 4s → ≤15 RPM
'''
    new_def = '''# —— AI 限速器（统一，已固化进 common.AIRateLimiter）——
# 旧方案（非线程安全 _gemini_last_call + 4s 最小间隔，踩 15 RPM 零余量）已废弃；
# 改用 common.ai_limiter：RPM 滑窗(默认10,留33%余量) + 每日预算(默认800) + 并发闸(默认1)。
'''
    _apply(path, old_def, new_def, "E1-旧限速定义")

    # E2: 替换 inline 限速块为统一限速器调用
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
    _apply(path, old_in, new_in, "E2-inline限速")

    # E3: report 前置初始化（④ 防全重试失败 NameError）
    old_init = '''        content = None
        for gen in range(1, GEN_RETRIES + 1):'''
    new_init = '''        content = None
        report = None  # ④ 防御：_call_ai 全重试失败也不 NameError，交由下方 report is None 分支安全跳过
        for gen in range(1, GEN_RETRIES + 1):'''
    _apply(path, old_init, new_init, "E3-report初始化")

    # E4: screen_signals 组间随机 5-10s 抖动（②）
    old_jit = '''        LOG.info("分批筛选第 %d/%d 批完成，本批耗时 %.1fs，返回 %d 条",
                 bi + 1, len(batches), time.time() - bs, len(picks))'''
    new_jit = '''        LOG.info("分批筛选第 %d/%d 批完成，本批耗时 %.1fs，返回 %d 条",
                 bi + 1, len(batches), time.time() - bs, len(picks))
        # ② 组间强制随机延时（5-10s）：进一步摊开请求，远离免费层限流墙
        if bi + 1 < len(batches):
            _jit = random.uniform(5, 10)
            LOG.info("组间随机延时 %.1fs（避免密集请求撞限流墙）", _jit)
            time.sleep(_jit)'''
    _apply(path, old_jit, new_jit, "E4-组间抖动")

    # E5: 增量持久化（① 崩溃续跑）
    old_merge = '''        merged = C.merge_reports(accumulated, report)'''
    new_merge = '''        merged = C.merge_reports(accumulated, report)
        # ① 增量持久化：合并后立即落盘 + 记录已生成信号进度，使中途崩溃重跑时
        #    能跳过已生成条目（existing_keys 基于已存日报去重），省下重烧的 token。
        try:
            C.save_json(daily_state_path, merged)
            _record_gen_progress(today, [sig.get("id") for sig in new_signals if sig.get("id")])
            LOG.info("增量检查点：已合并 %d 条落盘（崩溃续跑可直接跳过已生成信号）",
                     sum(len(merged.get("modules", {}).get(k, [])) for k in C.MODULES))
        except Exception as e:
            LOG.warning("增量检查点写盘失败（不影响主流程）: %s", e)'''
    _apply(path, old_merge, new_merge, "E5-增量落盘")

    # E6: _record_gen_progress 助手（插在 _emit_changed 前）
    old_emit = "def _emit_changed(changed):"
    new_emit = '''def _record_gen_progress(day, signal_ids):
    """① 记录本次已成功生成条目的来源信号 ID，供崩溃续跑观测/跳过。"""
    try:
        path = os.path.join(C.STATE_DIR, "gen_progress_%s.json" % day)
        prev = C.load_json(path, {})
        done = set(prev.get("done_ids", []))
        for sid in signal_ids:
            if sid:
                done.add(sid)
        C.save_json(path, {"day": day, "done_ids": sorted(done),
                           "updated": datetime.datetime.now().isoformat()})
    except Exception as e:
        LOG.warning("gen_progress 记录失败（不影响主流程）: %s", e)


def _emit_changed(changed):'''
    _apply(path, old_emit, new_emit, "E6-gen_progress助手")

    # E7: report is None 时仍落盘累积（④ 局部容错）
    old_none = '''        if report is None:
            # 骨架/空批次被跳过：无新增有效内容，保留已累积内容不发布
            _emit_changed(False)
            LOG.info("本次无有效新增内容，跳过发布（已累积 %d 条保留）。", acc_total)
            return'''
    new_none = '''        if report is None:
            # 骨架/空批次被跳过：无新增有效内容，保留已累积内容不发布
            try:
                C.save_json(daily_state_path, accumulated)
            except Exception:
                pass
            _emit_changed(False)
            LOG.info("本次无有效新增内容，跳过发布（已累积 %d 条保留）。", acc_total)
            return'''
    _apply(path, old_none, new_none, "E7-None落盘")

    # E8: _extract_json 围栏剥离升级（③ 任意语言/大小写）
    old_fence = '''    if "```" in s:
        s = re.sub(r"```(?:json)?\\s*", "", s)
        s = s.replace("```", "")'''
    new_fence = '''    if "```" in s:
        # 剥离任意语言标记的代码围栏（```json / ```JSON / ```python / 裸 ``` 等），
        # 兼容模型偶发添加的 Markdown 包裹，避免解析崩溃。
        s = re.sub(r"```[a-zA-Z]*\\s*", "", s, flags=re.I)
        s = s.replace("```", "")'''
    _apply(path, old_fence, new_fence, "E8-围栏剥离")


if __name__ == "__main__":
    patch_common()
    patch_report()
    print("APPLY_DEEP_OPTS_DONE")
