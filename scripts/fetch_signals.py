#!/usr/bin/env python3
"""步骤1：多源采集 + 增量去重 + 60天自动清理。
新增内容源 = 在 scripts/sources.json 里加一条记录（类型可选 rss / reddit_json /
html_zhongnianren / github_readme_diff），主流程无需改动。
"""
import os
import re
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()
UTC = datetime.timezone.utc
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _now_iso():
    return C.beijing_now().isoformat()


def _http_get(url, headers=None, timeout=20):
    import requests
    h = {"User-Agent": "Mozilla/5.0 (compatible; sidehustle-bot/1.0)"}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


# ---------------- 解析器 ----------------
def parse_rss(url, name):
    import feedparser
    d = feedparser.parse(url)
    out = []
    for e in d.entries[:100]:
        link = e.get("link") or e.get("id") or ""
        if not link:
            continue
        content = e.get("summary", "")
        if not content and e.get("content"):
            content = e["content"][0].get("value", "")
        pub = e.get("published") or e.get("updated") or _now_iso()
        out.append({
            "id": link,
            "source_name": name,
            "source_url": link,
            "title": (e.get("title") or "").strip(),
            "content": content.strip(),
            "published_at": pub,
        })
    return out


def parse_reddit_json(url, name):
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
            "title": (d.get("title") or "").strip(),
            "content": (d.get("selftext") or "").strip(),
            "published_at": pub,
        })
    return out


def parse_html_zhongnianren(url, name):
    """中年指南：优先走自描述 RSS；否则回退抓取站内文章链接。
    放宽链接匹配（/posts/、/post/、/articles/、/p/ 等），并改用 <a> 自身文本作标题，
    提升在不同页面结构下的抓取覆盖率与健壮性。"""
    from bs4 import BeautifulSoup
    r = _http_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    base = url.rstrip("/")
    # 1) 自描述 RSS / Atom
    for lk in soup.find_all("link", rel="alternate"):
        t = (lk.get("type") or "").lower()
        if "rss" in t or "atom" in t:
            href = lk.get("href") or ""
            if href:
                feed = href if href.startswith("http") else base + href
                try:
                    return parse_rss(feed, name)
                except Exception as e:
                    LOG.warning("中年指南 RSS 解析失败，回退 HTML: %s", e)
            break
    # 2) 回退：站内文章链接
    out = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"/(posts?|articles?|p)/", href):
            continue
        full = href if href.startswith("http") else base + href
        if full in seen or not full.startswith("http"):
            continue
        seen.add(full)
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 4:
            continue
        pub = _now_iso()
        # 就近时间戳：父容器内第一个带时间属性
        container = a.find_parent(["li", "article", "div", "section"]) or a.parent
        if container:
            for el in container.find_all(True):
                t = el.get("datetime") or el.get("title") or ""
                m = ISO_RE.search(t)
                if m:
                    pub = m.group(0)
                    break
        out.append({
            "id": full,
            "source_name": name,
            "source_url": full,
            "title": text[:160],
            "content": text,
            "published_at": pub,
        })
    return out


def parse_github_readme_diff(cfg):
    """对比 README 快照，只取新增的项目条目；冷启动仅播种不产出。"""
    name = cfg["name"]
    url = cfg["url"]
    snap_path = os.path.join(C.REPO_ROOT, cfg["snapshot"])
    try:
        cur = _http_get(url).text
    except Exception as e:
        LOG.warning("github_readme_diff 抓取失败 %s: %s", name, e)
        return []
    old = ""
    if os.path.exists(snap_path):
        with open(snap_path, "r", encoding="utf-8") as f:
            old = f.read()
    # 写回最新快照
    os.makedirs(os.path.dirname(snap_path), exist_ok=True)
    with open(snap_path, "w", encoding="utf-8") as f:
        f.write(cur)
    if not old.strip():
        LOG.info("%s 冷启动：已播种快照，今日不产出（从明天起增量）", name)
        return []
    old_lines = set(l.strip() for l in old.splitlines())
    new_lines = [l.strip() for l in cur.splitlines()
                 if l.strip() and l.strip() not in old_lines and "http" in l]
    out = []
    for line in new_lines[:80]:
        m = re.search(r"https?://[^\s\)\]]+", line)
        link = m.group(0) if m else cfg.get("anchor", url)
        # 名称：取第一个 markdown 链接文字或行首文字
        nm = re.search(r"\[([^\]]+)\]", line)
        title = nm.group(1) if nm else line.split("http")[0].strip(" -[]#")
        out.append({
            "id": "cid:" + (link or line)[:80],
            "source_name": name,
            "source_url": link,
            "title": title[:120],
            "content": line,
            "published_at": _now_iso(),
        })
    return out


PARSERS = {
    "rss": parse_rss,
    "reddit_json": parse_reddit_json,
    "html_zhongnianren": parse_html_zhongnianren,
    "github_readme_diff": parse_github_readme_diff,
}


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


def main():
    C.ensure_dirs()
    sources = C.load_json(C.SOURCES_FILE, [])
    seen = C.load_seen()
    cold = len(seen) == 0
    LOG.info("开始采集，源数量=%d，冷启动=%s", len(sources), cold)

    candidates = []
    src_status = {}  # 源可用性状态：供后续告警/巡检使用
    for cfg in sources:
        sid = cfg.get("id")
        stype = cfg.get("type")
        parser = PARSERS.get(stype)
        if not parser:
            LOG.warning("未知源类型 %s，跳过: %s", stype, cfg.get("id"))
            src_status[sid] = {"ok": False, "reason": "unknown_type", "got": 0, "fresh": 0}
            continue
        try:
            items = parser(cfg) if stype == "github_readme_diff" else parser(cfg["url"], cfg["name"])
        except Exception as e:
            LOG.warning("源 %s 抓取失败: %s", cfg.get("id"), e)
            src_status[sid] = {"ok": False, "reason": "fetch_error", "got": 0, "fresh": 0}
            continue
        # 去重（按 id）
        fresh = [it for it in items if it.get("id") and it["id"] not in seen]
        LOG.info("源 %s: 抓到 %d，新增 %d", cfg.get("id"), len(items), len(fresh))
        src_status[sid] = {"ok": True, "reason": "", "got": len(items), "fresh": len(fresh)}
        # 每个源限流，保证多样性并控制 AI 上下文长度
        fresh = fresh[: C.MAX_PER_SOURCE]
        candidates.extend(fresh)

    if cold:
        candidates.sort(key=lambda x: x.get("published_at", ""), reverse=True)
        candidates = candidates[: C.MAX_CANDIDATES]

    # 写入今日候选
    today = C.date_str()
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
                    {"date": today, "status": src_status})
        failed = [k for k, v in src_status.items() if not v["ok"]]
        if failed:
            LOG.warning("⚠️ 今日有 %d 个源抓取失败: %s", len(failed), failed)
    except Exception as e:
        LOG.warning("写源状态失败（不影响主流程）: %s", e)

    # 清理
    cleanup()
    LOG.info("完成。候选数=%d", len(candidates))


if __name__ == "__main__":
    main()
