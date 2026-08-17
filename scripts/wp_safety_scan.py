#!/usr/bin/env python3

"""发布后合规扫描（纵深防御 + 审计）：

1. 通过 WP REST API 找到当日「副业日报」已发布文章（或 env PUBLISHED_URL 指定）；

2. 抓取渲染后的 HTML，剥离标签后跑 ad_filter.safety_hard_filter；

3. 若命中博彩/引流/自推/占位符 → 立即 DELETE 该文章（force，不经过回收站）并 exit(1) 使 job 失败（触发企业微信告警）；

4. 否则打印 SAFE。



用法（由 daily-report.yml / roundup.yml 在发布步骤之后调用）：

  python scripts/wp_safety_scan.py

依赖环境变量：WP_URL, WP_USER, WP_APP_PASSWORD, FUYR_TODAY(可选, 默认今天)

"""

import os

import sys

import json

import base64

import re

import html as html_lib



try:

    import requests

except ImportError:

    sys.stderr.write("requests 未安装\n")

    sys.exit(2)



sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ad_filter as adf





def _auth_header(user, app_pw):

    return {

        "Authorization": "Basic " + base64.b64encode(f"{user}:{app_pw}".encode()).decode(),

        "Content-Type": "application/json",

    }





def _strip_tags(html_text):

    txt = re.sub(r"<[^>]+>", " ", html_text)

    txt = html_lib.unescape(txt)

    return re.sub(r"\s+", " ", txt)





def main():

    wp_url = os.environ.get("WP_URL", "https://dajiayouxuan.com").rstrip("/")

    user = os.environ.get("WP_USER", "tougao")

    app_pw = os.environ.get("WP_APP_PASSWORD", "")

    today = os.environ.get("FUYR_TODAY") or __import__("datetime").date.today().strftime("%Y-%m-%d")



    if not app_pw:

        sys.stderr.write("WP_APP_PASSWORD 未设置，跳过扫描（不阻断发布）\n")

        sys.exit(0)

    # 本运行未发布新文章（发布步骤显式置 FUYR_PUBLISHED=0）→ 跳过扫描，
    # 避免误删线上旧文 / 误报。仅当发布步骤确实发布了（FUYR_PUBLISHED=1 或显式 PUBLISHED_URL）
    # 或手动独立调用（未定义该变量，保留旧的『最新一篇』回退用于存量违规清理）时才扫描。
    if os.environ.get("FUYR_PUBLISHED") == "0":
        print("本运行未发布新文章（FUYR_PUBLISHED=0），跳过发布后扫描（不删除任何线上文章）", file=sys.stderr)
        sys.exit(0)



    auth = _auth_header(user, app_pw)

    # 1) 定位目标文章：优先 env PUBLISHED_URL（由发布步骤写入 GITHUB_ENV），

    #    否则取最新一篇已发布文章（daily / roundup 工作流发布后即扫描，最新即目标）。

    target_id = None

    target_link = os.environ.get("PUBLISHED_URL") or os.environ.get("PUBLISHED_URL_SCAN")

    if target_link:

        # 从 link 反查 id（用于删除）

        try:

            r = requests.get(f"{wp_url}/wp-json/wp/v2/posts",

                             params={"search": target_link.rstrip("/").split("/")[-1], "status": "publish", "per_page": 5},

                             headers=auth, timeout=30)

            if r.ok:

                for p in r.json():

                    if (p.get("link") or "").rstrip("/") == target_link.rstrip("/"):

                        target_id = p.get("id")

                        break

        except Exception:

            pass

    else:

        try:

            r = requests.get(f"{wp_url}/wp-json/wp/v2/posts",

                             params={"status": "publish", "per_page": 1, "orderby": "date", "order": "desc"},

                             headers=auth, timeout=30)

            if r.ok and r.json():

                p = r.json()[0]

                target_id = p.get("id")

                target_link = p.get("link")

        except Exception as e:

            sys.stderr.write(f"查询最新文章失败: {e}\n")

    if not target_link:

        sys.stderr.write("未定位到已发布文章，跳过扫描（不阻断）\n")

        sys.exit(0)



        # 2) 抓取正文：优先 WP API 的 content.rendered（不含主题导航/页脚，避免误判），
    #    失败再回退公开页 HTML。
    txt = ""

    try:
        pr = requests.get(f"{wp_url}/wp-json/wp/v2/posts/{target_id}",
                         params={"context": "view"}, headers=auth, timeout=30)
        if pr.ok:
            txt = _strip_tags((pr.json().get("content") or {}).get("rendered") or "")
    except Exception:
        txt = ""

    if not txt:
        try:
            html_text = requests.get(target_link, timeout=30).text
        except Exception as e:
            print(f"抓取文章页失败: {e}", file=sys.stderr)
            sys.exit(0)
        txt = _strip_tags(html_text)

    hits = adf.safety_hard_filter(txt)

    if hits:

        sys.stderr.write(f"⚠️ 发布后扫描发现违规残留: {hits}\n")

        sys.stderr.write(f"⚠️ 自动删除文章 ID={target_id}（force，不经过回收站）\n")

        try:

            d = requests.delete(f"{wp_url}/wp-json/wp/v2/posts/{target_id}",

                                params={"force": "true"}, headers=auth, timeout=30)

            if d.ok:

                sys.stderr.write("✅ 已删除违规文章\n")

            else:

                sys.stderr.write(f"❌ 删除失败 HTTP {d.status_code}: {d.text[:200]}\n")

        except Exception as e:

            sys.stderr.write(f"❌ 删除异常: {e}\n")

        sys.exit(1)  # 使 job 失败 → 触发企业微信告警



    print(f"✅ 发布后合规扫描通过（文章 ID={target_id}，无博彩/引流/自推残留）")

    sys.exit(0)





if __name__ == "__main__":

    main()

