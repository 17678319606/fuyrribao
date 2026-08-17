#!/usr/bin/env python3
"""步骤1：多源采集 + 增量去重 + 60天自动清理。
新增内容源 = 在 scripts/sources.json 里加一条记录（类型可选 rss / reddit_json /
github_readme_diff / github_trending），主流程无需改动。
"""
import os
import re
import sys
import json
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import source_manager as SM

LOG = C.get_logger()
UTC = datetime.timezone.utc
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _now_iso():
    return C.beijing_now().isoformat()


def _clean_html(text):
    """把 RSS/HTML 文本清洗为纯文本（保留换行），供 AI 与正文展示使用。
    处理被转义或原生的 <br>/<p> 换行、剥离其余标签、解码 HTML 实体、折叠空白。
    例：中年指南 RSS 的 <description> 内含字面 '<br>'（由 &lt;br&gt; 解出），
    须先转成换行再剥离，否则会作为乱码出现在正文/AI 输入里。"""
    if not text:
        return ""
    # 换行标签 → 换行符
    text = re.sub(r"</?p\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    # 剥离其余 HTML 标签（含被转义后仍为字面 <...> 的情况）
    text = re.sub(r"<[^>]+>", "", text)
    # 解码 HTML 实体（&amp; &lt; &nbsp; 等）
    try:
        import html as _html
        text = _html.unescape(text)
    except Exception:
        pass
    # 折叠空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_text(text):
    """单行文本清洗（标题等）：去标签 + 解码实体 + 折叠空白。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    try:
        import html as _html
        text = _html.unescape(text)
    except Exception:
        pass
    return re.sub(r"\s+", " ", text).strip()


def _short(url, n=64):
    return url if len(url) <= n else url[:n] + "…"


def _http_get(url, headers=None, timeout=(15, 90), retries=2, backoff=5):
    """带「超时元组 + 重试 + 退避」的 HTTP GET，专为慢源/抖动网络设计。

    - timeout: (connect, read)。默认连接 15s、读取 90s——RSS/rsshub/GitHub raw/
      trending 偶发慢，给足读取余量，避免被误判超时丢源；
    - 偶发超时 / 连接抖动 / 429 / 5xx 自动重试 retries 次，退避线性增长；
    - 4xx 其他（鉴权/参数错误）直接抛出，重试无意义；
    - 单源失败不影响整体（调用方 main 已按源 try/except 隔离）。
    """
    import requests
    h = {"User-Agent": "Mozilla/5.0 (compatible; sidehustle-bot/1.0)"}
    if headers:
        h.update(headers)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            t0 = time.time()
            r = requests.get(url, headers=h, timeout=timeout)
            r.raise_for_status()
            LOG.info("抓取成功 %s（耗时 %.1fs，第 %d/%d 次）", _short(url), time.time() - t0, attempt, retries)
            return r
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.InvalidURL) as e:
            last_err = e
            wait = backoff * attempt
            LOG.warning("抓取临时失败 %s（%s，第 %d/%d 次），%ds 后重试",
                        _short(url), type(e).__name__, attempt, retries, wait)
            if attempt < retries:
                time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            # 连接层失败（无响应体，status=0）/限流 429/5xx 均可重试；
            # 4xx 其他（鉴权/参数/404）才是真正非重试类，直接放弃。
            if status == 0 or status == 429 or 500 <= status < 600:
                last_err = e
                wait = backoff * attempt
                LOG.warning("抓取 HTTP %s %s（第 %d/%d 次），%ds 后重试",
                            status, _short(url), attempt, retries, wait)
                if attempt < retries:
                    time.sleep(wait)
            else:
                LOG.error("抓取 HTTP %s（非重试类）放弃: %s", status, _short(url))
                raise
    raise last_err or RuntimeError("http_get failed: " + url)


# ---------------- 解析器 ----------------
def parse_rss(cfg):
    """通用 RSS / Atom 解析。
    - 自动清洗 description/summary 里的 HTML（含被转义的字面 <br>），转为纯文本。
    - microblog=true 的源（如 Telegram 频道镜像）没有真正的标题，
      用正文首句作“引子标题”，避免卡片标题与正文完全重复堆叠。
    """
    import feedparser
    url = cfg["url"]
    name = cfg["name"]
    microblog = cfg.get("microblog", False)
    # 关键修复：RSS 不再交给 feedparser 自带 HTTP（超时完全不可控），
    # 改为先走受控 _http_get（90s 读取超时 + 重试），再 parseString。
    try:
        resp = _http_get(url)
        try:
            resp.encoding = resp.apparent_encoding or resp.encoding
        except Exception:
            pass
        raw = resp.text
    except Exception:
        # P1-C 修复：抓取失败必须让 main() 外层 except 知道（标 ok=False），
        # 不能吞掉返回空（否则失败源被误判为「成功、0 条」）。
        LOG.warning("RSS 抓取失败 %s", name)
        raise
    d = feedparser.parse(raw)
    out = []
    for e in d.entries[: C.MAX_PER_SOURCE]:
        link = e.get("link") or e.get("id") or ""
        if not link:
            continue
        content = e.get("summary", "")
        if not content and e.get("content"):
            content = e["content"][0].get("value", "")
        content = _clean_html(content)
        if microblog:
            first = content.split("\n")[0].strip()
            title = (first[:48] + "…") if len(first) > 48 else first
            if not title:
                title = name
        else:
            title = _clean_text(e.get("title") or "").strip()
        pub = e.get("published") or e.get("updated") or _now_iso()
        out.append({
            "id": link,
            "source_name": name,
            "source_url": link,
            "title": title,
            "content": content,
            "published_at": pub,
        })
    return out


def parse_reddit_json(cfg):
    url = cfg["url"]
    name = cfg["name"]
    r = _http_get(url, headers={"User-Agent": "sidehustle-bot/1.0"})
    j = r.json()
    out = []
    for c in j.get("data", {}).get("children", []):
        d = c.get("data", {})
        perm = d.get("permalink", "")
        link = "https://www.reddit.com" + perm if perm else ""
        if not link:
            continue
        created = d.get("created_utc")
        pub = datetime.datetime.fromtimestamp(created, UTC).isoformat() if created else _now_iso()
        out.append({
            "id": link,
            "source_name": name,
            "source_url": link,
            "title": _clean_text(d.get("title") or "").strip(),
            "content": _clean_html(d.get("selftext") or "").strip(),
            "published_at": pub,
        })
    return out


def parse_github_readme_diff(cfg):
    """对比 README 快照，只取新增的项目条目；冷启动仅播种不产出。

    快照仅保存「链接集合」（几 KB），不再保存 500KB 全文——
    避免每次运行把大文件提交进 git 导致仓库无限膨胀（容量优化）。
    """
    name = cfg["name"]
    url = cfg["url"]
    snap_path = os.path.join(C.REPO_ROOT, cfg["snapshot"])
    try:
        cur = _http_get(url).text
    except Exception:
        # P1-C 修复：抓取失败必须上抛，使 main() 标 ok=False（冷启动无新内容仍是合法空，见下方 return []）。
        LOG.warning("github_readme_diff 抓取失败 %s", name)
        raise
    # 抽取当前 README 中的 http 链接集合（去重、保序）
    cur_links = []
    seen_link = set()
    for line in cur.splitlines():
        line = line.strip()
        if not line or "http" not in line:
            continue
        m = re.search(r"https?://[^\s\)\]]+", line)
        if not m:
            continue
        link = m.group(0)
        if link in seen_link:
            continue
        seen_link.add(link)
        cur_links.append(link)
    # 读取旧快照（紧凑链接集合）
    old_links = set()
    if os.path.exists(snap_path):
        with open(snap_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    old_links.add(line)
    # 写回紧凑快照（仅链接，几 KB；不再提交大文件）
    os.makedirs(os.path.dirname(snap_path), exist_ok=True)
    with open(snap_path, "w", encoding="utf-8") as f:
        if cur_links:
            f.write("\n".join(cur_links) + "\n")
    if not old_links:
        LOG.info("%s 冷启动：已播种快照，今日不产出（从明天起增量）", name)
        return []
    new_links = [l for l in cur_links if l not in old_links]
    out = []
    for link in new_links[:80]:
        # 标题：在该行取第一个 markdown 链接文字，否则用链接本身
        title = link
        for line in cur.splitlines():
            if link in line:
                nm = re.search(r"\[([^\]]+)\]", line)
                if nm:
                    title = nm.group(1)[:120]
                break
        out.append({
            "id": "cid:" + link[:80],
            "source_name": name,
            "source_url": link,
            "title": title,
            "content": link,
            "published_at": _now_iso(),
        })
    return out


def parse_github_trending(cfg):
    """抓取 GitHub Trending 榜单（每日），提炼当天热门仓库作为副业/独立开发灵感源。"""
    url = cfg["url"]
    name = cfg["name"]
    from bs4 import BeautifulSoup
    try:
        r = _http_get(url)
    except Exception:
        # P1-C 修复：抓取失败必须上抛，使 main() 标 ok=False（合法空源仍由主流程记 ok=True, got=0）。
        LOG.warning("github_trending 抓取失败 %s", name)
        raise
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for art in soup.select("article.Box-row"):
        h2 = art.find("h2", class_=re.compile("lh-condensed"))
        a = h2.find("a") if h2 else None
        if not a:
            continue
        repo_path = (a.get("href") or "").strip()
        if not repo_path:
            continue
        repo_path = repo_path.strip("/")
        source_url = "https://github.com/" + repo_path
        p = art.find("p")
        desc = p.get_text(" ", strip=True) if p else ""
        lang_el = art.find("span", attrs={"itemprop": "programmingLanguage"})
        lang = lang_el.get_text(strip=True) if lang_el else ""
        stars = ""
        span = art.find("span", class_=re.compile("float-sm-right"))
        if span:
            stars = span.get_text(" ", strip=True)
        repo_title = repo_path.replace("/", " / ")
        content = (repo_title + "\n"
                   + (("语言: " + lang + "\n") if lang else "")
                   + ((stars + "\n") if stars else "")
                   + desc)
        out.append({
            "id": source_url,
            "source_name": name,
            "source_url": source_url,
            "title": repo_title + ((" — " + desc[:60]) if desc else ""),
            "content": content,
            "published_at": _now_iso(),
        })
    return out


PARSERS = {
    "rss": parse_rss,
    "reddit_json": parse_reddit_json,
    "github_readme_diff": parse_github_readme_diff,
    "github_trending": parse_github_trending,
}


# ───────────────────────── 广告 / 博彩 / 引流黑名单（零 LLM 预筛） ─────────────────────────
# TG / 论坛类源常混入博彩推广、引流软文；这类内容语义上像真内容，
# 仅靠 AI 守门易被放过（截图实锤：铂莱娱乐/贵宾会 750.cc 漏进精选）。
# 故在采集层用关键词+正则硬性拦截，零成本、零额度、永不送入 AI。
# 两级策略：
#   HARD（博彩/赌博/引流包装词）—— 标题或正文命中即删（unambiguous 黑产信号，零误杀）。
#   SOFT（联系方式/营销 CTA 词）—— 仅在【标题】出现才判广告；这些词（二维码/免费领/限时
#        福利/加微信）在合法文章正文与订阅 CTA 里高频出现，若对正文放行会误杀正常科技资讯
#        （实测曾把爱范儿 AI 新闻整批误删）。故 SOFT 仅作用于标题。
_AD_HARD_TERMS = [
    # 博彩/赌博（ unambiguous，命中即删）
    "博彩", "菠菜", "赌博", "娱乐城", "贵宾会", "棋牌", "百家乐", "老虎机",
    "投注", "下注", "盘口", "返水", "上押", "网投", "黑平台", "黑台", "野鸡",
    "开元", "永利", "威尼斯", "新葡京", "金沙", "视讯厅", "真人视讯", "信用盘",
    # 拉人/引流话术（包装成项目机会或观点，本质广告）
    # 注意：月入过万/日赚 等词在合法副业案例复盘里高频出现，误杀风险高，故不入硬名单；
    # 仅保留 unambiguous 的诈骗/博彩包装词（注册送/首充/稳赚不赔/一夜暴富/带你玩等）。
    "信誉担保", "全网担保", "集团背书", "全网首发", "强热启航",
    "注册送", "首充", "充值送", "稳赚不赔", "一夜暴富",
    "躺着赚", "稳赚", "包赚", "带赚", "导师带", "带你玩",
]
_AD_SOFT_TERMS = [
    # 联系方式 / 营销 CTA（仅标题判定，避免正文订阅 CTA 误杀正常内容）
    "加微信", "微信号", "扫码添加", "扫码领取", "二维码", "私聊客服", "小妹",
    "小哥哥带你", "一对一指导", "免费领", "限时福利",
]
_AD_HARD_RE = re.compile("|".join(re.escape(t) for t in _AD_HARD_TERMS))
_AD_SOFT_RE = re.compile("|".join(re.escape(t) for t in _AD_SOFT_TERMS))
# 典型垃圾短域名 + 担保话术组合（博彩站常用 .cc/.vip/.top 等）
_SPAM_DOMAIN = re.compile(r"[a-z0-9-]{3,}\.(cc|vip|top|xyz|asia|bet|casino|tv|club)\b", re.I)

def is_ad_spam(text, title=""):
    """高置信广告/博彩/引流信号 → True（应丢弃）。

    HARD 词（博彩/赌博/引流包装）命中标题或正文即删；
    SOFT 词（联系方式/营销 CTA）仅在标题出现才判广告，避免正文订阅 CTA 误杀正常内容。
    """
    if not text:
        return False
    # HARD：标题或正文任一命中即删（unambiguous 黑产信号）
    if _AD_HARD_RE.search(text):
        return True
    # SOFT：仅对标题生效（正文常见「扫码关注 / 免费领资料」等正常 CTA）
    if title and _AD_SOFT_RE.search(title):
        return True
    # 短域名 + 担保/送/赚 任一相伴 → 强广告信号（避免单独 .cc 误杀正常内容）
    if _SPAM_DOMAIN.search(text) and re.search(r"担保|送|赚|充值|开户|代理|邀请", text):
        return True
    return False

def filter_ads(signals):
    """就地过滤广告/博彩信号，返回 (保留列表, 丢弃数)。"""
    kept, dropped = [], 0
    for it in signals:
        title = it.get("title", "") or ""
        content = it.get("content", "") or ""
        if is_ad_spam(title + " " + content, title):
            dropped += 1
            LOG.warning("【广告拦截】丢弃疑似博彩/引流广告：%s | %s",
                        it.get("source_name", ""), (it.get("title") or "")[:50])
            continue
        kept.append(it)
    if dropped:
        LOG.warning("【广告拦截】本批共丢弃 %d 条广告/博彩/引流信号", dropped)
    return kept, dropped

def cleanup():
    """删除 RETENTION_DAYS 天前的数据文件，并裁剪去重表。"""
    import glob
    cutoff = C.beijing_now() - datetime.timedelta(days=C.RETENTION_DAYS)
    for pat in ("candidates-*.json", "report-*.json"):
        for fp in glob.glob(os.path.join(C.DATA_DIR, pat)):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(fp))
            if not m:
                continue
            try:
                d = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=C.BJ)
            except Exception:
                continue
            if d < cutoff:
                os.remove(fp)
                LOG.info("清理过期文件: %s", os.path.basename(fp))
    seen = C.load_seen()
    if seen:
        kept = {k: v for k, v in seen.items()
                if C.days_ago_iso(C.RETENTION_DAYS) <= (v or "")}
        removed = len(seen) - len(kept)
        if removed:
            C.save_seen(kept)
            LOG.info("去重表裁剪 %d 条（>%d天）", removed, C.RETENTION_DAYS)

    # 同日累积日报：state/ 走缓存，长期运行会累积 daily_report_<date>.json，需裁剪避免缓存膨胀。
    for fp in glob.glob(os.path.join(C.STATE_DIR, "daily_report_*.json")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(fp))
        if not m:
            continue
        try:
            d = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=C.BJ)
        except Exception:
            continue
        if d < cutoff:
            try:
                os.remove(fp)
                LOG.info("清理过期同日累积日报: %s", os.path.basename(fp))
            except OSError:
                pass


def main():
    C.ensure_dirs()
    sources = C.load_json(C.SOURCES_FILE, [])
    # 内容源管理系统：按综合打分预分配每源候选容量上限（高分源更高 cap，防垄断+提质量）
    _caps = SM.allocate_caps(sources)
    _id2cap = {s.get("id"): _caps.get(s.get("id")) for s in sources}
    seen = C.load_seen()
    quota_map = {s.get("name"): s.get("quota", "normal") for s in sources}  # P1-A：高配额源识别
    cold = len(seen) == 0
    today = C.date_str()
    LOG.info("开始采集，源数量=%d，冷启动=%s，今日=%s", len(sources), cold, today)

    # 死源黑名单（小黑屋）：手动 retired 或自动连续抓取失败达阈值的源在此列出，
    # 主循环跳过它们，避免反复 403 空耗 credit 与触发"源抓取失败"误告警。
    _dead_path = os.path.join(C.STATE_DIR, "dead_sources.json")
    _streak_path = os.path.join(C.STATE_DIR, "source_fail_streak.json")
    _dead_blacklist = set()
    try:
        _d = C.load_json(_dead_path, [])
        if isinstance(_d, list):
            _dead_blacklist = set(str(x) for x in _d)
    except Exception:
        pass
    _streak = {}
    try:
        _s = C.load_json(_streak_path, {})
        if isinstance(_s, dict):
            _streak = {str(k): int(v) for k, v in _s.items()}
    except Exception:
        pass
    DEAD_STREAK_THRESHOLD = 5

    candidates = []
    src_status = {}  # 源可用性状态：供后续告警/巡检使用
    for cfg in sources:
        sid = cfg.get("id")
        # 跳过已退役(retired) / 死源黑名单中的源（小黑屋）
        if cfg.get("status") == "retired" or sid in _dead_blacklist:
            LOG.info("源 %s 已退役/黑名单(blacklisted=%s, retired=%s)，跳过抓取。",
                     sid, sid in _dead_blacklist, cfg.get("status") == "retired")
            src_status[sid] = {"ok": True, "reason": "retired_or_blacklisted", "got": 0, "fresh": 0}
            continue
        # P2：跳过显式禁用 / CI 不可达的源（如 reddit JSON、被数据中心 IP 封的 feed），
        # 既避免无谓的 403 抓取与 credit 自愈空耗，也避免触发"源抓取失败"误告警。
        if cfg.get("enabled") is False or cfg.get("ci_blocked"):
            LOG.info("源 %s 已禁用(enabled=%s, ci_blocked=%s)，跳过抓取。",
                     sid, cfg.get("enabled"), cfg.get("ci_blocked"))
            src_status[sid] = {"ok": True, "reason": "disabled", "got": 0, "fresh": 0}
            continue
        stype = cfg.get("type")
        parser = PARSERS.get(stype)
        if not parser:
            LOG.warning("未知源类型 %s，跳过: %s", stype, cfg.get("id"))
            src_status[sid] = {"ok": False, "reason": "unknown_type", "got": 0, "fresh": 0}
            continue
        try:
            items = parser(cfg)
        except Exception as e:
            LOG.warning("源 %s 抓取失败: %s", cfg.get("id"), e)
            src_status[sid] = {"ok": False, "reason": "fetch_error", "got": 0, "fresh": 0}
            continue
        # 去重（按 id）：仅屏蔽「既往日」已见过的信号，允许同日重跑重新采集。
        # 这样「移到回收站后重跑 / 同日多次执行」都能重新产出候选，再由 publish 覆盖更新。
        fresh = []
        for it in items:
            iid = it.get("id")
            if not iid:
                continue
            last = seen.get(iid)
            if last and last[:10] < today:   # 仅既往日屏蔽；同日放行，支持重跑刷新
                continue
            fresh.append(it)
        LOG.info("源 %s: 抓到 %d，新增 %d", cfg.get("id"), len(items), len(fresh))
        # 注意：此处 src_status[sid] 用的是外层源 id（第 360 行），
        # 内层循环变量已改名为 iid，避免源 id 被条目 id 遮蔽（P1-B 修复）。
        src_status[sid] = {"ok": True, "reason": "", "got": len(items), "fresh": len(fresh)}
        # 按源综合打分分配每源候选容量上限（高分源更高 cap，防垄断 + 提质量）
        _cap = _id2cap.get(sid, C.MAX_PER_SOURCE)
        fresh = fresh[: _cap]
        for _it in fresh:
            _it["source_score"] = _cap
        candidates.extend(fresh)

    if cold:
        candidates.sort(key=lambda x: x.get("published_at", ""), reverse=True)
        candidates = candidates[: C.MAX_CANDIDATES]

    # 非冷启动也做总量护栏 + 多样性均衡（P1-A 修复）：
    # 候选超过 BALANCE_TRIGGER 即触发「按源均衡采样」，使单源占比可控、不再出现单源垄断
    # （如中年指南一度占 ~47%）；普通源单源上限 = ceil(BALANCE_TARGET / 组数)，
    # 高配额源（quota=="high"，如中年指南）给 HIGH_SOURCE_CAP，既防垄断又不过度砍增量源新内容；
    # 同时不浪费 AI 额度（候选总量收敛到 BALANCE_TARGET 附近）。
    if len(candidates) > C.BALANCE_TRIGGER:
        from collections import OrderedDict
        orig = len(candidates)
        groups = OrderedDict()
        for s in candidates:
            groups.setdefault(s.get("source_name", "未知"), []).append(s)
        n = len(groups)
        per = max(1, -(-C.BALANCE_TARGET // n))   # 普通源单源上限
        # 第一遍：按 cap 截断（高配额源用 HIGH_SOURCE_CAP），得到每源初选。
        capped = {}
        for nm, items in groups.items():
            cap = C.HIGH_SOURCE_CAP if quota_map.get(nm) == "high" else per
            capped[nm] = list(items[:cap])
        picked = [it for v in capped.values() for it in v]
        # 第二遍：总量未达 BALANCE_TARGET 且仍有空间时，仅向「未满」源按 cap 补位；
        # 绝不突破任何源的 cap（修复竞析实现缺陷：原 leftover 回填会把高配额源被砍的
        # 剩余又全部填回，导致垄断回潮、限流形同虚设）。
        room_total = C.BALANCE_TARGET - len(picked)
        if room_total > 0:
            for nm, items in groups.items():
                if room_total <= 0:
                    break
                cap = C.HIGH_SOURCE_CAP if quota_map.get(nm) == "high" else per
                have = len(capped[nm])
                if have >= cap:
                    continue
                extra = items[have:cap]
                take = min(len(extra), room_total)
                if take:
                    capped[nm].extend(extra[:take])
                    room_total -= take
        candidates = [it for v in capped.values() for it in v]
        candidates = candidates[: C.BALANCE_TARGET]
        LOG.info("候选 %d 条触发均衡（阈值 %d），按源采样至 %d 条（保多样性 + 控额度）",
                 orig, C.BALANCE_TRIGGER, len(candidates))

    # 广告 / 博彩 / 引流黑名单预筛（零 LLM，永不送入 AI；截图实锤漏过需硬拦）
    candidates, _dropped = filter_ads(candidates)
    if _dropped:
        LOG.info("广告预筛后候选数=%d（已删 %d）", len(candidates), _dropped)

    # 写入今日候选
    cand_path = os.path.join(C.DATA_DIR, f"candidates-{today}.json")
    C.save_json(cand_path, candidates)
    LOG.info("候选总数=%d，写入 %s", len(candidates), cand_path)

    # 更新去重表
    for it in candidates:
        seen[it["id"]] = C.beijing_now().isoformat()
    C.save_seen(seen)

    # 记录源可用性状态（供巡检/告警使用）
    try:
        C.save_json(os.path.join(C.STATE_DIR, "source_status.json"),
                    {"date": today, "status": src_status, "total_candidates": len(candidates)})
        failed = [k for k, v in src_status.items() if not v["ok"]]
        if failed:
            LOG.warning("⚠️ 今日有 %d 个源抓取失败: %s", len(failed), failed)
    except Exception as e:
        LOG.warning("写源状态失败（不影响主流程）: %s", e)

    # 源管理系统：记录当次每源指标（抓取成功率/产量），供打分与容量分配使用
    try:
        SM.record_run(src_status)
    except Exception as e:
        LOG.warning("源指标记录失败（不影响主流程）: %s", e)

    # 死源自动隔离：连续抓取失败达阈值（仅计 fetch_error）的源写入 dead_sources.json（小黑屋），
    # 下次运行由主循环跳过，避免长期 403 源反复空耗 credit 与触发"源抓取失败"误告警。
    try:
        _changed = False
        for _sid, _st in src_status.items():
            if _st.get("reason") == "fetch_error":
                _streak[_sid] = int(_streak.get(_sid, 0)) + 1
            else:
                _streak[_sid] = 0
            if _streak.get(_sid, 0) >= DEAD_STREAK_THRESHOLD and _sid not in _dead_blacklist:
                _dead_blacklist.add(_sid)
                _changed = True
                LOG.warning("⚠️ 源 %s 连续 %d 次抓取失败，已自动加入死源黑名单（小黑屋），"
                            "下次运行跳过。恢复请清理 state/dead_sources.json。", _sid, DEAD_STREAK_THRESHOLD)
        _streak = {k: v for k, v in _streak.items() if v > 0}
        C.save_json(_streak_path, _streak)
        if _changed:
            C.save_json(_dead_path, sorted(_dead_blacklist))
    except Exception as e:
        LOG.warning("死源黑名单更新失败（不影响主流程）: %s", e)

    # 清理
    cleanup()
    LOG.info("完成。候选数=%d", len(candidates))


if __name__ == "__main__":
    main()
