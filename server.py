# autoblog/server.py — v22.3(2.x): 별도 블로그 앱
# 공개 페이지(목록·상세·카테고리·태그·프리뷰) + 발행 API + SEO/AEO/GEO/GA4.
# 설계 원칙 (문서 11 Phase 2):
#   - SEO: sitemap·robots·RSS·canonical·OG+Twitter·JSON-LD(Article/FAQPage/Breadcrumb/WebSite)
#   - AEO: 한줄 요약·질문형 헤딩·FAQ 구조
#   - GEO: E-E-A-T 신호(발행/수정일·출처)·관련 글 위젯·외부 링크 nofollow
#   - 운세 인덱스 정책: 당일 운세만 index, 과거 운세 noindex (engine_meta 기준)
import datetime
import hmac
import json
import re
import time
from urllib.parse import quote

import bleach
import markdown as md
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

import config as config_mod
import db as db_mod

ALLOWED_TAGS = [
    "p", "br", "hr", "h1", "h2", "h3", "h4", "ul", "ol", "li",
    "strong", "em", "b", "i", "code", "pre", "blockquote", "a", "img",
    "table", "thead", "tbody", "tr", "th", "td", "span", "div",
]
ALLOWED_ATTRS = {"a": ["href", "title", "rel"], "img": ["src", "alt", "title"]}


class _RateLimiter:
    """고정 창 rate limit — 윈도우 내 호출 횟수 상한."""

    def __init__(self, limit, window):
        self.limit = limit
        self.window = window
        self.hits = []

    def allow(self):
        now = time.monotonic()
        self.hits = [t for t in self.hits if now - t < self.window]
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True


def render_markdown(body_md, base_url=""):
    """마크다운 → HTML (XSS 새니타이즈 + 외부 링크 nofollow)."""
    html = md.markdown(body_md or "", extensions=["extra", "tables", "fenced_code"])
    html = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
    # 외부 링크 rel="nofollow" (내부 링크는 그대로)
    def _link(match):
        href = match.group(1)  # group 2는 선택적 rel — None일 수 있음
        rel = ' rel="nofollow"' if href.startswith("http") else ""
        return f'<a href="{href}"{rel}>'
    return re.sub(r'<a href="([^"]*)"( rel="[^"]*")?>', _link, html)


def parse_engine_meta(post):
    try:
        meta = json.loads(post.get("engine_meta") or "{}")
        return meta if isinstance(meta, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def is_fortune_post(post):
    meta = parse_engine_meta(post)
    return bool(meta.get("fortune_type"))


def should_index(post, today):
    """운세 인덱스 정책: 당일 운세만 index, 과거 운세 noindex. 일반 글은 항상."""
    if not is_fortune_post(post):
        return True
    return parse_engine_meta(post).get("ref_date") == today.isoformat()


def create_app(cfg):
    app = FastAPI()
    # 발행 API는 autostudio(브라우저)에서 호출 — 설정된 origin 허용
    _cors = [o.strip() for o in str(cfg.get("cors_origins", "")).split(",") if o.strip()]
    if _cors:
        app.add_middleware(CORSMiddleware,
                           allow_origins=_cors,
                           allow_methods=["POST", "PATCH", "DELETE", "OPTIONS"],
                           allow_headers=["Authorization", "Content-Type"])
    state = {"db": None}
    # 발행 API rate limit — 분당 N회 (고정 창, 인메모리)
    limiter = _RateLimiter(int(cfg.get("publish_rate_limit", 60)), 60.0)

    def check_rate_limit():
        if not limiter.allow():
            raise HTTPException(status_code=429,
                                detail="rate limit exceeded (60/min)")

    def get_db():
        if state["db"] is None:
            d = db_mod.Database(cfg["db_url"])
            d.init()
            state["db"] = d
        return state["db"]

    def close_db():
        if state["db"] is not None:
            state["db"].close()
            state["db"] = None

    def require_token(authorization: str = Header(default="")):
        token = cfg.get("blog_token", "")
        if not token or not hmac.compare_digest(
                authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="invalid token")

    # ---------- 레이아웃 ----------

    def layout(title, body, og=None, head_extra="", data_attrs=""):
        ga4 = cfg.get("ga4_measurement_id", "")
        ga_script = (
            f'<script async src="https://www.googletagmanager.com/gtag/js?id={ga4}"></script>'
            f'<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments);}}'
            f'gtag("js",new Date());gtag("config","{ga4}");</script>'
            f'''<script>
(function(){{
  if (!window.gtag) return;
  var slug = document.body.getAttribute('data-slug');
  if (slug) gtag('event', 'view_article', {{slug: slug}});
  var fired = {{}};
  function onScroll(){{
    var h = document.documentElement;
    var p = (h.scrollTop + h.clientHeight) / h.scrollHeight;
    if (p >= 0.5 && !fired.s50) {{ fired.s50 = 1; gtag('event', 'scroll_50', {{slug: slug || ''}}); }}
    if (p >= 0.9 && !fired.s90) {{ fired.s90 = 1; gtag('event', 'scroll_90', {{slug: slug || ''}}); }}
  }}
  window.addEventListener('scroll', onScroll, {{passive: true}});
  document.addEventListener('click', function(e){{
    var a = e.target && e.target.closest ? e.target.closest('a') : null;
    if (!a) return;
    var href = a.getAttribute('href') || '';
    if (/^https?:\\/\\//.test(href) && href.indexOf(location.origin) !== 0) {{
      gtag('event', 'outbound_click', {{url: href}});
    }} else if (href.charAt(0) === '/') {{
      gtag('event', 'internal_click', {{url: href}});
    }}
  }});
}})();
</script>'''
            if ga4 else "")
        og = og or {}
        og_tags = ""
        for prop, val in og.items():
            og_tags += f'<meta property="{prop}" content="{_esc_attr(val)}">'
        return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>{CSS}</style>
{og_tags}{head_extra}{ga_script}</head><body{data_attrs}>
<header class="site"><div class="wrap">
  <a class="logo" href="/">MYEONG BLOG</a>
  <nav><a href="/">홈</a></nav>
</div></header>
<main class="wrap">{body}</main>
<footer class="site"><div class="wrap"><p>© MYEONG BLOG — 운세와 일상 정보</p></div></footer>
</body></html>"""

    def _esc(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;") \
            .replace(">", "&gt;").replace('"', "&quot;")

    def _esc_attr(s):
        return _esc(s).replace("'", "&#39;")

    # 사용자 입력(title 등)이 들어간 JSON-LD는 HTML 탈출 차단.
    # < > 를 \u003c \u003e 로 치환 — JSON 파서는 원문으로 복원, HTML 파서는 태그로 인식 불가
    def _safe_json(obj):
        return json.dumps(obj, ensure_ascii=False).replace(
            "<", "\\u003c").replace(">", "\\u003e")

    def _twitter_tags(og):
        t = [f'<meta name="twitter:card" content="summary_large_image">']
        for prop, val in og.items():
            t.append(f'<meta name="twitter:{prop[3:]}" content="{_esc_attr(val)}">')
        return "".join(t)

    # ---------- 공개 페이지 ----------

    @app.get("/")
    def index(request: Request, page: int = 1, category: str = "", tag: str = ""):
        d = get_db()
        page = max(1, page)
        posts, total = d.list_posts(category=category, tag=tag, page=page, page_size=10)
        cards = "".join(_post_card(p) for p in posts) or (
            '<p class="empty">게시물이 없습니다.</p>')
        def _qparam(name, value):
            return f"&{name}={quote(value)}" if value else ""
        cats = "".join(f'<a class="chip" href="/?category={quote(c["category"])}">'
                       f'{_esc(c["category"])} ({c["c"]})</a>'
                       for c in d.list_categories())
        tags = "".join(f'<a class="chip" href="/?tag={quote(t)}">#{_esc(t)}</a>'
                       for t, _ in d.list_tags()[:12])
        max_page = max(1, (total + 9) // 10)
        prev_link = (f'<a class="btn" href="/?page={page - 1}{_qparam("category", category)}'
                     f'{_qparam("tag", tag)}">이전</a>' if page > 1 else
                     '<span class="btn off">이전</span>')
        next_link = (f'<a class="btn" href="/?page={page + 1}{_qparam("category", category)}'
                     f'{_qparam("tag", tag)}">다음</a>' if page < max_page else
                     '<span class="btn off">다음</span>')
        pager = (f'<div class="pager">{prev_link}<span>{page}/{max_page}</span>'
                 f'{next_link}</div>' if max_page > 1 else "")
        body = f"""
<section class="hero"><h1>운세와 정보, 매일 새롭게</h1>
<p>오늘의 운세부터 생활 정보까지 — 검색과 AI 답변에 인용되는 글을 씁니다.</p></section>
<section class="chips">{cats}{tags}</section>
<section class="grid">{cards}</section>{pager}"""
        base = cfg["base_url"]
        og = {"og:title": "MYEONG BLOG — 운세와 정보",
              "og:type": "website", "og:url": base,
              "og:site_name": "MYEONG BLOG",
              "og:description": "오늘의 운세부터 생활 정보까지 — 검색과 AI 답변에 인용되는 글을 씁니다."}
        org = _safe_json({
            "@context": "https://schema.org", "@type": "Organization",
            "name": "MYEONG BLOG", "url": base,
            "logo": f"{base}/favicon.ico"})
        website = _safe_json({
            "@context": "https://schema.org", "@type": "WebSite",
            "name": "MYEONG BLOG", "url": base,
            "potentialAction": {"@type": "SearchAction",
                                "target": {"@type": "EntryPoint",
                                           "urlTemplate": f"{base}/?q={{search_term_string}}"},
                                "query-input": "required name=search_term_string"}})
        head_extra = (_twitter_tags(og)
                      + f'<link rel="canonical" href="{base}">\n{org}\n{website}')
        return HTMLResponse(layout("MYEONG BLOG — 운세와 정보", body,
                                   og, head_extra))

    def _post_card(p):
        meta = parse_engine_meta(p)
        badge = f'<span class="badge">{_esc(meta.get("fortune_type", "운세") if is_fortune_post(p) else "정보")}</span>'
        return f"""<article class="card">
  <a class="thumb" href="/{_esc_attr(p['slug'])}">
    {"<img src=\"" + _esc_attr(p['image_url']) + "\" alt=\"\">" if p.get('image_url') else '<div class="ph"></div>'}
  </a>
  <div class="card-body">
    <div class="meta">{badge}<time>{_esc((p['published_at'] or p['created_at'])[:10])}</time></div>
    <h2><a href="/{_esc_attr(p['slug'])}">{_esc(p['title'])}</a></h2>
    <p class="excerpt">{_esc(_excerpt(p['body_md']))}</p>
  </div>
</article>"""

    def _excerpt(body_md, limit=140):
        text = re.sub(r"[#>*`\[\]()|-]", " ", body_md or "")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit] + ("..." if len(text) > limit else "")

    # ---------- SEO (2.4) — 정적 경로는 /{slug}보다 먼저 등록 (라우트 우선순위) ----------

    @app.get("/sitemap.xml")
    def sitemap():
        d = get_db()
        today = datetime.date.today()
        base = cfg["base_url"]
        urls = [f"<url><loc>{base}/</loc></url>"]
        page = 1
        while True:
            posts, _ = d.list_posts(page=page, page_size=500)
            if not posts:
                break
            for p in posts:
                if not should_index(p, today):
                    continue  # 당일 운세만 — 과거 운세는 noindex
                urls.append(f"<url><loc>{base}/{p['slug']}</loc>"
                            f"<lastmod>{(p['updated_at'] or p['published_at'] or '')[:10]}</lastmod></url>")
            if len(posts) < 500:
                break
            page += 1
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               + "".join(urls) + "</urlset>")
        return Response(content=xml, media_type="application/xml")

    @app.get("/robots.txt")
    def robots():
        base = cfg["base_url"]
        return Response(content=f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n",
                        media_type="text/plain")

    @app.get("/rss.xml")
    def rss():
        d = get_db()
        posts, _ = d.list_posts(page=1, page_size=20)
        base = cfg["base_url"]
        items = "".join(
            f"<item><title>{_esc(p['title'])}</title><link>{base}/{p['slug']}</link>"
            f"<pubDate>{_rss_date(p['published_at'] or p['created_at'])}</pubDate>"
            f"<description>{_esc(_excerpt(p['body_md'], 200))}</description></item>"
            for p in posts)
        xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<rss version="2.0"><channel><title>MYEONG BLOG</title>'
               f'<link>{base}</link><description>운세와 일상 정보</description>'
               f"{items}</channel></rss>")
        return Response(content=xml, media_type="application/rss+xml")

    def _rss_date(iso):
        try:
            return datetime.datetime.fromisoformat(iso).strftime(
                "%a, %d %b %Y %H:%M:%S +0900")
        except (ValueError, TypeError):
            return ""

    @app.get("/{slug}")
    def detail(slug: str, request: Request):
        d = get_db()
        post = d.get_post_by_slug(slug, status="published")
        if not post:
            return HTMLResponse(layout("404", '<p class="empty">글을 찾을 수 없습니다.</p>'),
                                status_code=404)
        body_html = render_markdown(post["body_md"], cfg["base_url"])
        meta = parse_engine_meta(post)
        index = should_index(post, datetime.date.today())
        robots = "" if index else '<meta name="robots" content="noindex">'
        base = cfg["base_url"]
        url = f"{base}/{post['slug']}"
        date_pub = (post["published_at"] or post["created_at"])[:10]
        og = {
            "og:title": post["title"],
            "og:type": "article",
            "og:url": url,
            "og:site_name": "MYEONG BLOG",
        }
        if post.get("image_url"):
            og["og:image"] = post["image_url"]
        # JSON-LD: Article — 사용자 입력(title 등)의 HTML 탈출 차단 (see _safe_json).
        jsonld = _safe_json({
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": post["title"],
            "datePublished": date_pub,
            "dateModified": (post["updated_at"] or date_pub)[:10],
            "author": {"@type": "Organization", "name": "MYEONG BLOG", "url": base},
            "publisher": {"@type": "Organization", "name": "MYEONG BLOG", "url": base},
            "mainEntityOfPage": {"@type": "WebPage", "@id": url},
            "speakable": {"@type": "SpeakableSpecification",
                          "cssSelector": [".post h1", ".post .content h2"]},
        })
        breadcrumb = _safe_json({
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "홈", "item": base},
                {"@type": "ListItem", "position": 2, "name": post["title"], "item": url},
            ],
        })
        # FAQPage — engine_meta.faq(질문/답변 목록)가 전달되면 구조화 (AEO)
        faq_jsonld = ""
        faq_list = meta.get("faq") if isinstance(meta, dict) else None
        if isinstance(faq_list, list) and faq_list:
            faq_jsonld = _safe_json({
                "@context": "https://schema.org", "@type": "FAQPage",
                "mainEntity": [
                    {"@type": "Question", "name": str(f["q"]),
                     "acceptedAnswer": {"@type": "Answer", "text": str(f["a"])}}
                    for f in faq_list if isinstance(f, dict) and f.get("q") and f.get("a")]
            })
        related = d.related_posts(post)
        related_html = ""
        if related:
            items = "".join(
                f'<li><a href="/{_esc_attr(r["slug"])}">{_esc(r["title"])}</a></li>'
                for r in related)
            related_html = f'<section class="related"><h2>관련 글</h2><ul>{items}</ul></section>'
        tags_html = "".join(
            f'<a class="chip" href="/?tag={quote(t)}">#{_esc(t)}</a>'
            for t in (post["tags"] or "").split(",") if t.strip())
        body = f"""
<article class="post">
  <div class="meta"><time>{date_pub}</time> · 수정 {_esc((post['updated_at'] or date_pub)[:10])}
  {f" · <a href='/category/{quote(post['category'])}'>{_esc(post['category'])}</a>" if post.get('category') else ""}</div>
  <h1>{_esc(post['title'])}</h1>
  <div class="chips">{tags_html}</div>
  <div class="content">{body_html}</div>
</article>{related_html}"""
        head_extra = (_twitter_tags(og)
                      + f"{robots}\n<link rel=\"canonical\" href=\"{url}\">\n"
                      + f"{jsonld}\n{breadcrumb}" + (f"\n{faq_jsonld}" if faq_jsonld else ""))
        return HTMLResponse(layout(post["title"], body, og, head_extra,
                                   f' data-slug="{_esc_attr(post["slug"])}"'))

    @app.get("/category/{category}")
    def category_page(category: str):
        d = get_db()
        posts, _ = d.list_posts(category=category)
        cards = "".join(_post_card(p) for p in posts) or '<p class="empty">게시물이 없습니다.</p>'
        return HTMLResponse(layout(f"{category} - MYEONG BLOG",
                                   f"<h1 class='page-title'>{_esc(category)}</h1><section class='grid'>{cards}</section>"))

    @app.get("/tag/{tag}")
    def tag_page(tag: str):
        d = get_db()
        posts, _ = d.list_posts(tag=tag)
        cards = "".join(_post_card(p) for p in posts) or '<p class="empty">게시물이 없습니다.</p>'
        return HTMLResponse(layout(f"#{tag} - MYEONG BLOG",
                                   f"<h1 class='page-title'>#{_esc(tag)}</h1><section class='grid'>{cards}</section>"))

    @app.get("/preview/{slug}")
    def preview(slug: str, token: str = ""):
        # draft 미리보기 — 발행 토큰 일치 시만
        if not cfg.get("blog_token") or not hmac.compare_digest(token, cfg["blog_token"]):
            raise HTTPException(status_code=401, detail="invalid token")
        d = get_db()
        post = d.get_post_by_slug(slug)
        if not post:
            raise HTTPException(status_code=404, detail="not found")
        body_html = render_markdown(post["body_md"])
        return HTMLResponse(layout(f"[미리보기] {post['title']}",
                                   f"<article class='post'><h1>{_esc(post['title'])}</h1>"
                                   f"<div class='content'>{body_html}</div></article>"))

    # ---------- 발행 API (2.7) ----------

    @app.post("/api/posts")
    async def create_post(request: Request, authorization: str = Header(default="")):
        require_token(authorization)
        check_rate_limit()
        return await _apply_post(request)

    @app.patch("/api/posts/{slug}")
    async def update_post(slug: str, request: Request,
                          authorization: str = Header(default="")):
        require_token(authorization)
        check_rate_limit()
        return await _apply_post(request, slug)

    @app.delete("/api/posts/{slug}")
    def delete_post(slug: str, authorization: str = Header(default="")):
        require_token(authorization)
        check_rate_limit()
        d = get_db()
        d.delete_post(slug)
        return {"ok": True}

    async def _apply_post(request, slug=None):
        raw = (await request.body()).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid json")
        if not slug:
            slug = str(data.get("slug", "")).strip()
        if not slug or not re.fullmatch(r"[a-z0-9-]+", slug):
            raise HTTPException(status_code=400, detail="slug must be [a-z0-9-]+")
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))) \
            .isoformat(timespec="seconds")
        d = get_db()
        existing = d.get_post_by_slug(slug)
        if existing:
            # PATCH: 전달된 키만 갱신, 나머지는 기존 값 유지 (status·tags·발행시각 보존)
            title = str(data.get("title", existing["title"])).strip()
            body_md = str(data.get("body", data.get("body_md", existing["body_md"]))).strip()
            tags = ",".join(str(t).strip() for t in (data.get("tags") or [])
                            if str(t).strip()) if "tags" in data else existing["tags"]
            category = str(data.get("category", existing["category"])).strip()
            image_url = str(data.get("image_url", existing["image_url"])).strip()
            engine_meta = data.get("engine_meta", existing["engine_meta"])
            engine_meta = json.dumps(engine_meta, ensure_ascii=False) \
                if isinstance(engine_meta, dict) else str(engine_meta)
            published_at = str(data.get("published_at", "")).strip() or \
                (existing["published_at"] or now)
        else:
            title = str(data.get("title", "")).strip()
            body_md = str(data.get("body", data.get("body_md", ""))).strip()
            tags = ",".join(str(t).strip() for t in (data.get("tags") or [])
                            if str(t).strip())
            category = str(data.get("category", "")).strip()
            image_url = str(data.get("image_url", "")).strip()
            engine_meta = json.dumps(data.get("engine_meta") or {}, ensure_ascii=False)
            published_at = str(data.get("published_at", "")).strip() or now
        if not title or not body_md:
            raise HTTPException(status_code=400, detail="title/body required")
        op, pid = d.upsert_post(
            slug, title, body_md,
            tags=tags, category=category, image_url=image_url,
            engine_meta=engine_meta, published_at=published_at, updated_at=now)
        # status 키가 없으면 현재 상태 유지 (PATCH가 draft를 공개로 바꾸지 않도록)
        status = str(data.get("status", "")).strip()
        if status in ("draft", "published"):
            d.set_post_status(slug, status, published_at=published_at, updated_at=now)
        elif op == "created":
            status = "published"
            d.set_post_status(slug, status, published_at=published_at, updated_at=now)
        else:
            status = existing["status"]
        return {"op": op, "id": pid, "slug": slug, "status": status}

    return app


CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: 'Pretendard Variable', Pretendard, -apple-system, sans-serif;
  color: #1f2937; background: #fafafa; line-height: 1.75; }
.wrap { max-width: 900px; margin: 0 auto; padding: 0 20px; }
header.site { background: #fff; border-bottom: 1px solid #e5e7eb; position: sticky; top: 0; z-index: 10; }
header.site .wrap { display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; }
.logo { font-weight: 800; letter-spacing: .08em; color: #111827; text-decoration: none; }
nav a { color: #6b7280; text-decoration: none; font-size: 14px; }
.hero { padding: 56px 0 32px; }
.hero h1 { font-size: 34px; margin: 0 0 8px; letter-spacing: -0.02em; }
.hero p { color: #6b7280; margin: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
.chip { display: inline-block; padding: 3px 10px; border-radius: 999px; background: #f3f4f6;
  color: #374151; font-size: 12px; text-decoration: none; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 6px; background: #eef2ff;
  color: #4f46e5; font-size: 11px; font-weight: 700; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 18px; margin: 20px 0; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; overflow: hidden; transition: box-shadow .2s; }
.card:hover { box-shadow: 0 8px 24px rgba(0,0,0,.07); }
.thumb img { width: 100%; height: 150px; object-fit: cover; }
.thumb .ph { width: 100%; height: 150px; background: linear-gradient(135deg, #eef2ff, #fdf2f8); }
.card-body { padding: 14px 16px 18px; }
.card-body h2 { font-size: 16px; margin: 6px 0; }
.card-body h2 a { color: #111827; text-decoration: none; }
.meta { display: flex; gap: 8px; align-items: center; color: #9ca3af; font-size: 12px; }
.excerpt { color: #6b7280; font-size: 13px; margin: 6px 0 0; }
.pager { display: flex; justify-content: center; gap: 12px; align-items: center; padding: 20px 0; }
.btn { display: inline-block; padding: 8px 16px; border: 1px solid #d1d5db; border-radius: 8px;
  background: #fff; color: #374151; text-decoration: none; font-size: 13px; }
.btn.off { color: #d1d5db; pointer-events: none; }
.post { background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; padding: 28px 32px; margin: 24px 0; }
.post h1 { font-size: 28px; margin: 12px 0 8px; letter-spacing: -0.02em; }
.content { margin-top: 20px; }
.content h2 { font-size: 21px; margin: 32px 0 12px; padding-bottom: 8px; border-bottom: 2px solid #f3f4f6; }
.content h3 { font-size: 17px; margin: 24px 0 8px; }
.content a { color: #4f46e5; }
.content img { max-width: 100%; border-radius: 10px; }
.content table { border-collapse: collapse; width: 100%; margin: 16px 0; }
.content th, .content td { border: 1px solid #e5e7eb; padding: 8px 12px; font-size: 14px; }
.content blockquote { border-left: 4px solid #e0e7ff; margin: 16px 0; padding: 8px 16px; background: #f8faff; }
.content pre { background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 10px; overflow-x: auto; }
.related { background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; padding: 20px 24px; margin: 20px 0; }
.related li { margin: 6px 0; }
.related a { color: #4f46e5; text-decoration: none; }
.empty { color: #9ca3af; text-align: center; padding: 48px 0; }
.page-title { font-size: 24px; margin: 32px 0 8px; }
footer.site { border-top: 1px solid #e5e7eb; margin-top: 40px; padding: 20px 0; color: #9ca3af; font-size: 12px; }
"""


app = create_app(config_mod.load_config())
