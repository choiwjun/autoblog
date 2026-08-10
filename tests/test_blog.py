# tests/test_blog.py — v22.3(2.x): 별도 블로그 앱 검증
# 발행 API(멱등·인증) · 공개 페이지 · 운세 인덱스 정책 · SEO · XSS
import datetime
import json

import pytest
from fastapi.testclient import TestClient

import config as config_mod
import db as db_mod
from server import create_app, should_index

AUTH = {"Authorization": "Bearer sekret"}


def make_app(tmp_path, env="development"):
    dbfile = f"sqlite:///{tmp_path / 'blog.db'}"
    cfg = config_mod.load_config()
    cfg.update({"db_url": dbfile, "blog_token": "sekret", "env": env,
                "base_url": "http://test.local"})
    return create_app(cfg)


def _publish(client, slug="hello-world", **over):
    data = {
        "slug": slug, "title": "첫 글",
        "body": "## 소제목\n본문 내용입니다.\n\n## 자주 묻는 질문\n### 질문\n답변",
        "tags": ["운세", "일상"], "category": "정보",
        "image_url": "https://cdn.example.com/a.png",
        "engine_meta": {},
    }
    data.update(over)
    return client.post("/api/posts", json=data, headers=AUTH)


def test_publish_api_create_update_delete(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = _publish(client)
    assert r.status_code == 200
    assert r.json()["op"] == "created"
    # 멱등 update (PATCH 의미 — slug 중복 시 updated)
    r2 = _publish(client, title="수정된 제목")
    assert r2.json()["op"] == "updated"
    assert r2.json()["id"] == r.json()["id"]
    # PATCH 경로
    r3 = client.patch("/api/posts/hello-world",
                      json={"title": "패치 제목", "body": "패치 본문"},
                      headers=AUTH)
    assert r3.json()["op"] == "updated"
    # DELETE
    assert client.delete("/api/posts/hello-world", headers=AUTH).json()["ok"] is True
    assert client.get("/hello-world").status_code == 404


def test_publish_api_auth_and_validation(tmp_path):
    client = TestClient(make_app(tmp_path, env="production"))
    # 토큰 없음 → 401 (AUTH 헤더 미첨부 요청)
    assert client.post("/api/posts", json={"slug": "ok-1", "title": "t",
                                           "body": "b"}).status_code == 401
    r = client.post("/api/posts", json={"slug": "ok-1", "title": "t", "body": "b"},
                    headers=AUTH)
    assert r.status_code == 200
    # 잘못된 slug / 필수 누락
    assert client.post("/api/posts",
                       json={"slug": "Bad Slug!", "title": "t", "body": "b"},
                       headers=AUTH).status_code == 400
    assert client.post("/api/posts", json={"slug": "ok-2", "title": ""},
                       headers=AUTH).status_code == 400


def test_public_pages_and_rendering(tmp_path):
    client = TestClient(make_app(tmp_path))
    _publish(client, slug="hello-world")
    r = client.get("/")
    assert r.status_code == 200
    assert "첫 글" in r.text and "MYEONG BLOG" in r.text
    detail = client.get("/hello-world")
    assert detail.status_code == 200
    assert "<h1>첫 글</h1>" in detail.text
    assert "소제목" in detail.text
    # XSS — script 태그 제거 (잔존 텍스트는 무해 — 실행 불가)
    client.post("/api/posts", json={
        "slug": "xss-test", "title": "<script>alert(1)</script>제목",
        "body": "본문 <script>alert(2)</script>"}, headers=AUTH)
    safe = client.get("/xss-test").text
    assert "<script" not in safe
    # 외부 링크 nofollow
    client.post("/api/posts", json={
        "slug": "link-test", "title": "링크", "body": "[외부](https://example.com)"},
        headers=AUTH)
    assert 'rel="nofollow"' in client.get("/link-test").text


def test_fortune_index_policy(tmp_path):
    # 당일 운세만 index — 과거 운세는 noindex + sitemap 제외
    today = datetime.date.today().isoformat()
    client = TestClient(make_app(tmp_path))
    _publish(client, slug=f"fortune-{today}",
             engine_meta={"fortune_type": "daily", "ref_date": today})
    _publish(client, slug="fortune-2020-01-01",
             engine_meta={"fortune_type": "daily", "ref_date": "2020-01-01"})
    _publish(client, slug="normal-post", engine_meta={})
    # noindex 메타
    assert "noindex" not in client.get(f"/fortune-{today}").text
    assert "noindex" in client.get("/fortune-2020-01-01").text
    assert "noindex" not in client.get("/normal-post").text
    # sitemap — 당일 운세·일반만
    sitemap = client.get("/sitemap.xml").text
    assert f"/fortune-{today}" in sitemap
    assert "/fortune-2020-01-01" not in sitemap
    assert "/normal-post" in sitemap
    # robots.txt
    assert "Sitemap:" in client.get("/robots.txt").text
    # RSS
    assert "fortune-" in client.get("/rss.xml").text


def test_related_posts_by_tag(tmp_path):
    client = TestClient(make_app(tmp_path))
    for i in range(3):
        _publish(client, slug=f"post-{i}", title=f"글 {i}",
                 tags=["공통"], body=f"## 섹션 {i}\n본문")
    detail = client.get("/post-0")
    assert "관련 글" in detail.text
    assert "post-1" in detail.text and "post-2" in detail.text
    import re as _re
    _after = detail.text.split("관련 글")[1].split("</ul>")[0]
    assert "post-0" not in _re.findall(r'href="/(post-\d)"', _after)


def test_preview_requires_token(tmp_path):
    client = TestClient(make_app(tmp_path))
    _publish(client, slug="draft-post", status="draft")
    assert client.get("/preview/draft-post").status_code == 401
    assert client.get("/preview/draft-post?token=sekret").status_code == 200
    # 발행 전 공개 페이지에는 미노출
    assert "draft-post" not in client.get("/").text


def test_should_index_units():
    today = datetime.date.today()
    normal = {"engine_meta": "{}"}
    fortune_today = {"engine_meta": json.dumps(
        {"fortune_type": "daily", "ref_date": today.isoformat()})}
    fortune_old = {"engine_meta": json.dumps(
        {"fortune_type": "daily", "ref_date": "2020-01-01"})}
    assert should_index(normal, today)
    assert should_index(fortune_today, today)
    assert not should_index(fortune_old, today)


def test_patch_keeps_status_and_published_at(tmp_path):
    client = TestClient(make_app(tmp_path))
    _publish(client, slug="draft-a", status="draft",
             published_at="2026-01-02T00:00:00+09:00")
    _publish(client, slug="pub-a", published_at="2026-01-03T00:00:00+09:00")
    # PATCH(본문만) — draft 유지 + 공개 페이지 미노출
    r = client.patch("/api/posts/draft-a", json={"title": "제목만"},
                     headers=AUTH)
    assert r.json()["status"] == "draft"
    assert client.get("/draft-a").status_code == 404
    # published_at 유지 (DB 직접 조회)
    d = db_mod.Database(f"sqlite:///{tmp_path / 'blog.db'}")
    assert d.get_post_by_slug("draft-a")["published_at"] == "2026-01-02T00:00:00+09:00"
    assert d.get_post_by_slug("pub-a")["published_at"] == "2026-01-03T00:00:00+09:00"
    d.close()


def test_db_migration_and_categories(tmp_path):
    d = db_mod.Database(f"sqlite:///{tmp_path / 't.db'}")
    d.init()
    d.upsert_post("a-1", "A", "본문", tags="t1", category="보험",
                  published_at="2026-08-01", updated_at="2026-08-01")
    d.set_post_status("a-1", "published")
    d.upsert_post("a-2", "B", "본문", tags="t2", category="정보",
                  published_at="2026-08-02", updated_at="2026-08-02")
    d.set_post_status("a-2", "published")
    rows, total = d.list_posts()
    assert total == 2 and len(rows) == 2
    cats = {c["category"] for c in d.list_categories()}
    assert cats == {"보험", "정보"}
    related = d.related_posts(d.get_post_by_slug("a-1"))
    assert related == []  # 태그 불일치
    d.close()
