#!/usr/bin/env python3
"""自主内容源发现（长期自动化扩源）。

按 docs/source-standard.md 的五维标准（相关度/稳定性/格式/稀缺性/权威度）打分，
对候选源自动校验 + 评分，达标者写入 scripts/sources.json（实现"AI 自己写入内容源"），
不达标者仅记录到 state/source_discovery_log.json 供人工审阅。

设计约束（与标准一致）：
  - 不自建源：只接入现成 RSS/Atom/官方 JSON/社区公开 feed。
  - 免费额度内：仅用 GitHub Search API（自带额度）+ 璇玑 LLM 打分（用户自有网关，成本极低）。
  - 韧性：LLM 不可用时退回纯启发式打分，不阻断引入；任何源实拉取校验失败即拒。
  - 只改文件、不碰 git；提交由流水线步骤完成。

用法：
  python scripts/discover_sources.py                  # 例行发现（定时/手动）
  python scripts/discover_sources.py --dry-run        # 只评估不写入
  python scripts/discover_sources.py --opml feeds.opml  # 导入 OPML 并自动评估纳入
"""
import os
import sys
import json
import re
import time
import datetime
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

# 综合准入阈值（与 docs/source-standard.md 一致）
ACCEPT_COMPOSITE = 16          # 综合分 ≥ 16/25（略放宽，纳入高质量小众源）
ACCEPT_MIN_DIM = 3             # 每个维度 ≥ 3（防单维劣质源混入）
REVIEW_COMPOSITE = 12          # 12–15 仅记录待审（扩待审网，人工可捞）

# 主题相关度词表（副业/AI/独立开发/增长/创业）
TOPIC_LEXICON = [
    "副业", "side hustle", "side-hustle", "indie", "独立开发", "创业", "saas",
    "no-code", "无代码", "ai", "人工智能", "大模型", "llm", "llm", "growth",
    "增长", "变现", "monetiz", "passive income", "被动收入", "freelance", "自由职业",
    "self-host", "自托管", "developer", "开发者", "startup", "开源", "github",
    "工具", "product", "产品", "自动化", "automation", "newsletter", "博客",
    "blog", "hacker", "技术", "营销", "获客", "内容创作", "creator",
]

# 权威域分级（命中得 4–5；未命中默认 3）
AUTHORITY_TIERS = {
    "github.com": 5, "reddit.com": 4, "news.ycombinator.com": 5, "lobste.rs": 5,
    "dev.to": 4, "producthunt.com": 4, "indiehackers.com": 4, "sspai.com": 4,
    "ruanyifeng.com": 5, "tmtpost.com": 3, "ifanr.com": 4, "oschina.net": 4,
    "qbitai.com": 4, "jiqizhixin.com": 4, "geekpark.net": 4, "woshipm.com": 4,
    "growthhackers.com": 4, "appinn.com": 4, "v2ex.com": 4, "w2solo.com": 5,
    "smashingmagazine.com": 5,
}

# 稳定性分（按 type + 是否需要鉴权）
STABILITY_BY_TYPE = {
    "rss": 5, "reddit_json": 4, "github_trending": 4, "github_readme_diff": 4,
}

# 人工精选的高相关候选种子（复用现有解析器类型，零新增代码）。
# 这些社区/聚合源"相关性高 + 官方/社区性质强 + 免费 + 标准协议"，契合"聚合社交媒体博主"诉求。
CANDIDATE_SEEDS = [
    {"id": "reddit_sideproject", "type": "reddit_json",
     "url": "https://www.reddit.com/r/SideProject/hot.json?limit=25",
     "name": "Reddit r/SideProject", "rationale": "独立开发者项目展示社区，高相关"},
    {"id": "reddit_indiehackers", "type": "reddit_json",
     "url": "https://www.reddit.com/r/indiehackers/hot.json?limit=25",
     "name": "Reddit r/indiehackers", "rationale": "indie hacker 社区"},
    {"id": "reddit_entrepreneur", "type": "reddit_json",
     "url": "https://www.reddit.com/r/Entrepreneur/hot.json?limit=25",
     "name": "Reddit r/Entrepreneur", "rationale": "创业/副业讨论社区"},
    {"id": "reddit_artificial", "type": "reddit_json",
     "url": "https://www.reddit.com/r/artificial/hot.json?limit=25",
     "name": "Reddit r/artificial", "rationale": "AI 资讯社区，高相关"},
    {"id": "reddit_localllama", "type": "reddit_json",
     "url": "https://www.reddit.com/r/LocalLLaMA/hot.json?limit=25",
     "name": "Reddit r/LocalLLaMA", "rationale": "LLM/AI 工具实践"},
    {"id": "reddit_selfhosted", "type": "reddit_json",
     "url": "https://www.reddit.com/r/selfhosted/hot.json?limit=25",
     "name": "Reddit r/selfhosted", "rationale": "自托管/技术副业"},
    {"id": "hn_show", "type": "rss", "url": "https://hnrss.org/show",
     "name": "Hacker News Show", "rationale": "HN 深度长文，质量高（同 host 已验证可用）"},
    {"id": "smashing", "type": "rss", "url": "https://www.smashingmagazine.com/feed/",
     "name": "Smashing Magazine", "rationale": "前端/工程高质量博客"},
]

# 人工策展的高相关常驻种子（替代"reeddaily 自动爬取"——reeddaily 的 OPML/feed 在登录
# 鉴权后，免费 cron 无法批量拉取；故改为：由我们（或用户提供 OPML）手动精选优质 feed
# 常驻为种子，持续经同一套 validate+score 管线评估，达标即纳入。零新增运行时成本。）
CURATED_SEEDS = [
    {"id": "hn_best", "type": "rss", "url": "https://hnrss.org/best",
     "name": "Hacker News Best", "rationale": "HN 最高赞长文，质量天花板"},
    {"id": "theresanaiforthat", "type": "rss", "url": "https://theresanaiforthat.com/feed/",
     "name": "There's An AI For That", "rationale": "AI 工具聚合，强相关"},
    {"id": "lennys", "type": "rss", "url": "https://www.lennysnewsletter.com/feed",
     "name": "Lenny's Newsletter", "rationale": "产品/增长/职业高质量 newsletter"},
    {"id": "yc_blog", "type": "rss", "url": "https://www.ycombinator.com/blog/rss.xml",
     "name": "Y Combinator Blog", "rationale": "顶级孵化器官方博客"},
    {"id": "stackdiary", "type": "rss", "url": "https://stackdiary.com/feed/",
     "name": "Stack Diary", "rationale": "AI/生产力工具实战评测"},
    {"id": "saastr", "type": "rss", "url": "https://www.saastr.com/feed/",
     "name": "SaaStr", "rationale": "SaaS/创业增长高质量博客"},
    {"id": "indiehackers_blog", "type": "rss", "url": "https://www.indiehackers.com/blog/rss",
     "name": "Indie Hackers Blog", "rationale": "独立开发者官方博客"},
    {"id": "spi", "type": "rss", "url": "https://www.smartpassiveincome.com/feed/",
     "name": "Smart Passive Income", "rationale": "被动收入/副业经典 newsletter"},
]


def _host_of(url):
    m = re.match(r"https?://([^/]+)/?", url or "")
    return (m.group(1) or "").lower() if m else ""


def _topic_hits(text):
    t = (text or "").lower()
    return sum(1 for w in TOPIC_LEXICON if w.lower() in t)


def _relevance_from_hits(hits):
    if hits >= 3:
        return 5
    if hits == 2:
        return 4
    if hits == 1:
        return 3
    return 1


def _authority_from_host(host):
    for domain, score in AUTHORITY_TIERS.items():
        if domain in host:
            return score
    return 3


def validate_source(cfg):
    """实拉取校验候选源是否可被解析为 feed。返回 (valid, format_score, sample_titles)。"""
    import requests
    url = cfg.get("url", "")
    stype = cfg.get("type", "")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; sidehustle-bot/1.0)"}
    try:
        r = requests.get(url, headers=headers, timeout=(15, 60))
        r.raise_for_status()
        raw = r.text
    except Exception as e:
        LOG.warning("校验失败 %s: %s", cfg.get("id"), e)
        return False, 1, []

    if stype == "reddit_json":
        try:
            j = r.json()
            kids = j.get("data", {}).get("children", [])
            if not kids:
                return False, 1, []
            titles = [k.get("data", {}).get("title", "") for k in kids[:8]]
            return True, 4, [t for t in titles if t]
        except Exception:
            return False, 1, []
    # rss / atom / 其他：用 feedparser 校验
    try:
        import feedparser
        d = feedparser.parse(raw)
        entries = d.entries[:8]
        if not entries:
            return False, 1, []
        titles = []
        for e in entries:
            t = e.get("title") or ""
            if t:
                titles.append(t)
        return True, 5, titles
    except Exception:
        # 退化：看原始是否含 RSS/Atom 标记
        if re.search(r"<rss|<feed|<\?xml", raw[:2000], re.I):
            return True, 4, []
        return False, 1, []


def github_search_candidates():
    """best-effort：用 GitHub Search API 找高 star 仓库的 homepage（可能为博客/feed）。"""
    import requests
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    topics = ["side-hustle", "indie-hacker", "ai-tools", "saas", "no-code", "passive-income",
              "solopreneur", "developer-tools"]
    out = []
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    for tp in topics:
        try:
            q = f"topic:{tp} stars:>30"
            r = requests.get("https://api.github.com/search/repositories",
                             params={"q": q, "per_page": 10, "sort": "stars"},
                             headers=headers, timeout=20)
            if r.status_code != 200:
                continue
            for it in r.json().get("items", []):
                home = (it.get("homepage") or "").strip()
                if home and home.startswith("http"):
                    out.append({
                        "id": "gh_" + str(it.get("id", "")),
                        "type": "rss",  # 待校验：仅当 homepage 确为 feed 才通过 validate
                        "url": home,
                        "name": it.get("full_name", home),
                        "rationale": "GitHub Search: topic=%s, stars=%s" % (tp, it.get("stargazers_count")),
                    })
        except Exception as e:
            LOG.warning("GitHub Search 失败(topic=%s): %s", tp, e)
        time.sleep(1)  # 尊重 GitHub 速率限制
    return out


def parse_opml(path):
    """解析 OPML（1.0/2.0，兼容嵌套 outline 与 RDF/ATTR 变体），产出候选源列表。

    只取含 xmlUrl 的 outline（即真实 feed），按 url 去重；其余（纯目录节点）跳过。
    返回的每条候选会进入与 CANDIDATE_SEEDS 完全相同的 validate+score 管线，达标即纳入。
    这是"reeddaily 策展 RSS → 纳入"诉求的落地形态：用户从 reeddaily/任意阅读器导出
    OPML，运行 `python discover_sources.py --opml xxx.opml` 即可自动评估纳入。零新增运行时成本。
    """
    try:
        raw = open(path, "r", encoding="utf-8", errors="ignore").read()
    except Exception as e:
        LOG.warning("读取 OPML 失败 %s: %s", path, e)
        return []
    out, seen = [], set()
    for m in re.finditer(r"<outline\b([^>]*)/?>", raw, re.I | re.S):
        attrs = m.group(1)

        def _attr(name):
            mm = re.search(r'%s\s*=\s*["\']([^"\']*)["\']' % re.escape(name), attrs, re.I)
            return mm.group(1).strip() if mm else ""

        xmlurl = _attr("xmlUrl")
        if not xmlurl:
            continue
        xmlurl = xmlurl.strip()
        if xmlurl in seen:
            continue
        seen.add(xmlurl)
        host = _host_of(xmlurl)
        name = _attr("title") or _attr("text") or host
        # id 同时含 host 前缀 + url 哈希，避免同 host 多 feed（如 /feed 与 /comments/feed）冲突被静默丢源
        uid = "opml_%s_%s" % (
            re.sub(r"[^a-z0-9]+", "_", host.lower()).strip("_") or "h",
            hashlib.md5(xmlurl.encode("utf-8")).hexdigest()[:10],
        )
        out.append({
            "id": uid,
            "type": "rss",
            "url": xmlurl,
            "name": name,
            "rationale": "OPML 导入（%s）" % os.path.basename(path),
            "added_by": "opml-import",
        })
    LOG.info("OPML %s 解析出 %d 个候选 feed", path, len(out))
    return out


def _call_llm_score(url, name, sample_titles):
    """用璇玑网关对候选源做五维精评。失败返回 None（退回启发式）。"""
    import requests
    base = os.environ.get("ai_base_url", "").strip() or os.environ.get("AI_BASE_URL", "").strip() \
        or "https://ai.jinbufenzi.com/v1"
    base = base.rstrip("/")
    api_key = (os.environ.get("AI_API_KEY", "").strip() or os.environ.get("ai_api_key", "").strip()
               or os.environ.get("AI_SIDEHUSTLE_API_KEY", "").strip())
    if not api_key:
        return None
    model = os.environ.get("AI_MODEL", "").strip() or os.environ.get("ai_model", "").strip() or "auto"
    sample = " | ".join(sample_titles[:5])
    sys_p = ("你是内容源质量评审。按五维（相关度/稳定性/格式/稀缺性/权威度）对内容源打分，"
             "每维 1-5 整数。只输出一个 JSON："
             '{"relevance":int,"stability":int,"format":int,"scarcity":int,"authority":int,"reason":"..."}。'
             "标准见：相关度=与副业/AI/独立开发/增长/创业契合度；稳定性=源可用抗变能力；"
             "格式=RSS/Atom=5、JSON=4、需爬JS=1；稀缺性=现有源未覆盖=高；权威度=官方/知名社区/资深作者=高。")
    user_p = (f"源名称：{name}\n源地址：{url}\n样例标题：{sample}\n"
              f"请据此评分并给一句话理由。")
    try:
        r = requests.post(base + "/chat/completions",
                          headers={"Authorization": "Bearer " + api_key,
                                   "Content-Type": "application/json"},
                          json={"model": model, "messages": [
                              {"role": "system", "content": sys_p},
                              {"role": "user", "content": user_p}],
                              "temperature": 0.2, "max_tokens": 300}, timeout=60)
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        return {k: int(d.get(k, 3)) for k in
                ("relevance", "stability", "format", "scarcity", "authority")} | \
               {"reason": str(d.get("reason", ""))}
    except Exception as e:
        LOG.warning("LLM 打分失败（退回启发式）: %s", e)
        return None


def score_candidate(cfg, existing_hosts, existing_names):
    """返回 (dims:dict, accepted:bool, tier:str, reason:str)。"""
    valid, fmt_score, titles = validate_source(cfg)
    if not valid:
        return None, False, "rejected", "校验未通过（无法解析为 feed）"
    host = _host_of(cfg.get("url", ""))
    name = cfg.get("name", "")
    # L0 启发式
    hits = _topic_hits(host + " " + name + " " + " ".join(titles))
    relevance = _relevance_from_hits(hits)
    stability = STABILITY_BY_TYPE.get(cfg.get("type", ""), 3)
    fmt = fmt_score
    authority = _authority_from_host(host)
    scarcity = 1 if (host in existing_hosts or name.lower() in existing_names) else 4
    dims = {"relevance": relevance, "stability": stability, "format": fmt,
            "scarcity": scarcity, "authority": authority}
    reason = "启发式"
    # L1 LLM 精评（覆盖 relevance/authority）
    llm = _call_llm_score(cfg.get("url", ""), name, titles)
    if llm:
        dims["relevance"] = llm.get("relevance", relevance)
        dims["authority"] = llm.get("authority", authority)
        reason = llm.get("reason", "LLM")
    composite = sum(dims.values())
    dims["composite"] = composite
    min_dim = min(dims[k] for k in ("relevance", "stability", "format", "scarcity", "authority"))
    if min_dim >= ACCEPT_MIN_DIM and composite >= ACCEPT_COMPOSITE:
        return dims, True, "accepted", reason
    if composite >= REVIEW_COMPOSITE:
        return dims, False, "review", reason
    return dims, False, "rejected", reason


def main():
    C.ensure_dirs()
    dry = "--dry-run" in sys.argv
    sources = C.load_json(C.SOURCES_FILE, [])
    existing_ids = {s.get("id") for s in sources}
    existing_urls = {s.get("url") for s in sources}
    existing_hosts = {_host_of(s.get("url", "")) for s in sources}
    existing_names = {s.get("name", "").lower() for s in sources}

    # 候选池 = 社区种子 + 人工策展常驻种子 + GitHub Search + (可选)OPML 导入
    candidates = list(CANDIDATE_SEEDS) + list(CURATED_SEEDS) + github_search_candidates()
    if "--opml" in sys.argv:
        try:
            opml_path = sys.argv[sys.argv.index("--opml") + 1]
            candidates += parse_opml(opml_path)
        except Exception as e:
            LOG.warning("OPML 参数解析失败: %s", e)
    # 去重：已在 sources.json 中的跳过
    candidates = [c for c in candidates
                  if c.get("id") not in existing_ids and c.get("url") not in existing_urls]

    # 活跃源软上限守卫：active+trial 已达 SOURCE_ACTIVE_CAP 时，新源只评估不写入（防 OPML 批量溢出）
    metrics = C.load_json(C.SOURCE_METRICS_FILE, {})
    active_n = sum(1 for s in sources
                   if metrics.get(s.get("id"), {}).get("status", "active") in ("active", "trial", "legacy"))

    log = {"date": C.date_str(), "candidates": len(candidates), "accepted": [], "review": [], "rejected": []}
    added = 0
    for c in candidates:
        dims, ok, tier, reason = score_candidate(c, existing_hosts, existing_names)
        if dims is None:
            log["rejected"].append({"id": c.get("id"), "url": c.get("url"), "why": reason})
            LOG.info("✗ %s 拒绝：%s", c.get("id"), reason)
            continue
        entry = {"id": c.get("id"), "url": c.get("url"), "name": c.get("name"),
                 "type": c.get("type"), "scores": dims, "reason": reason}
        if ok:
            if active_n >= C.SOURCE_ACTIVE_CAP:
                # 超活跃上限：降级为待审，不写入（避免 OPML/批量导入冲爆目标源数）
                log["review"].append(entry)
                LOG.info("• %s 达标但活跃源已达上限(%d)，转待审不写入", c.get("id"), C.SOURCE_ACTIVE_CAP)
                continue
            new_cfg = {"id": c["id"], "type": c["type"], "url": c["url"], "name": c["name"],
                       "added_by": c.get("added_by", "auto-discover"),
                       "added_date": C.date_str(),
                       "score": dims["composite"]}
            sources.append(new_cfg)
            existing_ids.add(c["id"]); existing_urls.add(c["url"])
            existing_hosts.add(_host_of(c["url"])); existing_names.add(c["name"].lower())
            active_n += 1
            log["accepted"].append(entry)
            added += 1
            LOG.info("✓ %s 自动引入（综合 %d，%s）", c.get("id"), dims["composite"], reason)
        elif tier == "review":
            log["review"].append(entry)
            LOG.info("• %s 待审（综合 %d，%s）", c.get("id"), dims["composite"], reason)
        else:
            log["rejected"].append(entry)
            LOG.info("✗ %s 拒绝（综合 %d，%s）", c.get("id"), dims["composite"], reason)

    C.save_json(os.path.join(C.STATE_DIR, "source_discovery_log.json"), log)
    if added and not dry:
        C.save_json(C.SOURCES_FILE, sources)
        LOG.info("已写入 %d 个新源到 sources.json", added)
    else:
        LOG.info("无新源写入（dry=%s, added=%d）", dry, added)
    print("DISCOVER_CHANGES=%d" % added)


if __name__ == "__main__":
    main()
