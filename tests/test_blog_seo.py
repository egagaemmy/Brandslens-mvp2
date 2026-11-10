"""The four new SEO blog posts and the technical SEO of the blog: what is published, what the pages look like to a search engine, the sitemap,
and that nothing written into a title, description or author can break a page."""
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import pytest
from sqlalchemy import select

from app import main
from app.blog_seo_posts import SEO_POSTS
from app.models import BlogPost, now_utc

SECRET = "test-secret"
BLOG = main.BLOG_URL
EXISTING_TITLES = ["How to Choose the Best Media Monitoring Tool for Your Brand in 2026", "The Hidden Cost of Not Tracking Online Mentions: A Guide for African Brands",
                   "Brand Impersonation and Fraud: How to Detect and Respond Before It Costs You", "From Detection to Resolution: Building a Real Crisis Escalation Workflow"]
EXISTING_SLUGS = [main._slugify(t) for t in EXISTING_TITLES]
NEW_SLUGS = [main._slugify(p["title"]) for p in SEO_POSTS]
ALLOWED_TAGS = {"p", "h2", "h3", "ul", "ol", "li", "b", "a"}


def words(html: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", html).split())


class Head(HTMLParser):
    """What a search engine reads: the title, the meta tags, the canonical link, the structured data, and the headings."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.canonical, self.ld, self.h1s = "", {}, "", [], []
        self._in, self._buf = None, ""
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            self.meta[a.get("name") or a.get("property")] = a.get("content")
        elif tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href")
        elif tag in ("title", "h1") or (tag == "script" and a.get("type") == "application/ld+json"):
            self._in, self._buf = tag, ""
    def handle_data(self, data):
        if self._in:
            self._buf += data
    def handle_endtag(self, tag):
        if tag == self._in:
            {"title": lambda: setattr(self, "title", self._buf), "h1": lambda: self.h1s.append(self._buf), "script": lambda: self.ld.append(self._buf)}[tag]()
            self._in = None


def parse(html: str) -> Head:
    h = Head(); h.feed(html); return h


@pytest.fixture
def published(client):
    r = client.get("/api/setup/seed-blog-seo", params={"secret": SECRET})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- the publishing route
def test_the_route_needs_the_setup_secret(client, db):
    assert client.get("/api/setup/seed-blog-seo", params={"secret": "wrong"}).status_code == 403
    assert client.get("/api/setup/seed-blog-seo").status_code in (403, 422)
    assert db.scalars(select(BlogPost)).all() == []


def test_it_publishes_exactly_the_four_posts_with_their_addresses(client, db, published):
    assert [c["slug"] for c in published["created"]] == NEW_SLUGS and len(NEW_SLUGS) == 4
    assert all(c["url"] == f"{BLOG}/blog/{c['slug']}" for c in published["created"]) and published["sitemap"] == f"{BLOG}/sitemap.xml"
    posts = db.scalars(select(BlogPost)).all()
    assert len(posts) == 4 and all(p.status == "published" and p.published_at and p.author_name == "BrandsLens Team" for p in posts)


def test_calling_it_again_creates_nothing_and_changes_nothing(client, db, published):
    before = {p.slug: (p.title, p.body_html, p.published_at) for p in db.scalars(select(BlogPost)).all()}
    again = client.get("/api/setup/seed-blog-seo", params={"secret": SECRET}).json()
    assert again["created"] == [] and again["already_published"] == NEW_SLUGS and "nothing to do" in again["message"]
    db.expire_all()
    assert {p.slug: (p.title, p.body_html, p.published_at) for p in db.scalars(select(BlogPost)).all()} == before


def test_it_leaves_the_four_launch_posts_alone_and_the_two_routes_do_not_clash(client, db, published):
    assert client.get("/api/setup/seed-blog", params={"secret": SECRET}).status_code == 200
    slugs = {p.slug for p in db.scalars(select(BlogPost)).all()}
    assert slugs == set(EXISTING_SLUGS) | set(NEW_SLUGS) and len(slugs) == 8


# ---------------------------------------------------------------- the writing itself
def test_every_post_is_long_structured_and_distinct():
    assert len({p["title"] for p in SEO_POSTS}) == 4 and len(set(NEW_SLUGS)) == 4 and not set(NEW_SLUGS) & set(EXISTING_SLUGS)
    for p in SEO_POSTS:
        assert words(p["body_html"]) >= 1000, p["title"]
        assert len(re.findall(r"<h2>", p["body_html"])) >= 6, p["title"]
        assert "<h1" not in p["body_html"], "the page supplies the one h1"
        assert len(p["title"]) <= 50 and 100 <= len(p["meta_description"]) <= 155 and 80 <= len(p["excerpt"]) <= 220, p["title"]


def test_titles_descriptions_and_excerpts_hold_no_quotes_brackets_or_ampersands():
    for p in SEO_POSTS:
        for field in ("title", "meta_description", "excerpt"):
            assert not re.search(r'["<>&]', p[field]), (p["title"], field)


def test_the_body_uses_only_plain_safe_tags_and_no_scripts_or_handlers():
    for p in SEO_POSTS:
        assert set(re.findall(r"</?([a-z0-9]+)", p["body_html"])) <= ALLOWED_TAGS, p["title"]
        assert not re.search(r"on\w+\s*=|javascript:|<script|<iframe|style=", p["body_html"], re.I), p["title"]
        assert all(re.fullmatch(r'<a href="[^"]+">', t) for t in re.findall(r"<a\b[^>]*>", p["body_html"])), p["title"]


def test_every_link_goes_to_a_page_that_exists():
    ok = re.compile(r"^(https://www\.brandslens\.com/(product|pricing)|https://app\.brandslens\.com/demo|/blog/([a-z0-9-]+))$")
    known = set(EXISTING_SLUGS) | set(NEW_SLUGS)
    for p in SEO_POSTS:
        links = re.findall(r'href="([^"]+)"', p["body_html"])
        assert links and all(ok.match(h) for h in links), (p["title"], links)
        for h in links:
            if h.startswith("/blog/"):
                assert h.removeprefix("/blog/") in known and h.removeprefix("/blog/") != main._slugify(p["title"]), (p["title"], h)
        assert any("brandslens.com/product" in h for h in links) and any("brandslens.com/pricing" in h for h in links) and any(h.endswith("/demo") for h in links), p["title"]


def test_the_brand_speaks_as_we_not_as_a_founder_and_names_no_platform_it_does_not_scan():
    for p in SEO_POSTS:
        text = re.sub(r"<[^>]+>", " ", p["body_html"]) + " " + p["title"] + " " + p["meta_description"] + " " + p["excerpt"]
        assert not re.search(r"\b(I|I'm|I've|I'll|my|me)\b", text), (p["title"], re.findall(r"\b(I|I'm|I've|I'll|my|me)\b", text))
        assert not re.search(r"founder|Emmy|Egaga", text, re.I), p["title"]
        assert not re.search(r"Instagram|TikTok|Facebook|WhatsApp|Snapchat|LinkedIn", text, re.I), p["title"]
        assert "\u2014" not in text and "\u2013" not in text, p["title"]


def test_nothing_in_the_posts_promises_what_the_product_does_not_do():
    text = " ".join(re.sub(r"<[^>]+>", " ", p["body_html"]) for p in SEO_POSTS).lower()
    for claim in ("free trial", "guarantee", "100%", "instantly", "in real time", "real-time monitoring", "real-time coverage", "never miss", "every mention"):
        assert claim not in text, claim
    assert not re.search(r"\b\d{2,3}\s?%|\bper cent of (all|brands|companies|customers)", text), "no invented statistics"


# ---------------------------------------------------------------- the pages, as a search engine reads them
def test_each_post_page_is_complete_for_search(client, db, published):
    for p, slug in zip(SEO_POSTS, NEW_SLUGS):
        r = client.get(f"/blog/{slug}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        h = parse(r.text)
        assert h.title == f"{p['title']} | BrandsLens" and len(h.title) <= 60, h.title
        assert h.meta["description"] == p["meta_description"]
        assert h.meta["robots"] == "index,follow,max-image-preview:large"
        assert h.canonical == f"{BLOG}/blog/{slug}"
        assert h.meta["og:title"] == h.title and h.meta["og:url"] == h.canonical and h.meta["og:type"] == "article" and h.meta["og:site_name"] == "BrandsLens"
        assert h.h1s == [p["title"]], "exactly one h1, the post title"
        assert r.text.count("<h2>") == p["body_html"].count("<h2>")


def test_each_post_page_carries_valid_structured_data(client, db, published):
    for p, slug in zip(SEO_POSTS, NEW_SLUGS):
        h = parse(client.get(f"/blog/{slug}").text)
        assert len(h.ld) == 1
        d = json.loads(h.ld[0])
        assert d["@context"] == "https://schema.org" and d["@type"] == "BlogPosting" and d["headline"] == p["title"] and d["description"] == p["meta_description"]
        assert d["url"] == d["mainEntityOfPage"]["@id"] == f"{BLOG}/blog/{slug}"
        assert d["author"] == {"@type": "Organization", "name": "BrandsLens Team"} and d["publisher"]["name"] == "BrandsLens"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+(\+00:00|Z)?", d["datePublished"]) and d["dateModified"]


def test_the_links_between_posts_open_real_posts(client, db, published):
    client.get("/api/setup/seed-blog", params={"secret": SECRET})
    for p in SEO_POSTS:
        for href in re.findall(r'href="(/blog/[a-z0-9-]+)"', p["body_html"]):
            r = client.get(href)
            assert r.status_code == 200 and "Post not found" not in r.text, (p["title"], href)


def test_the_listing_shows_the_new_posts_with_their_links(client, db, published):
    r = client.get("/blog")
    assert r.status_code == 200
    for p, slug in zip(SEO_POSTS, NEW_SLUGS):
        assert f'href="/blog/{slug}"' in r.text and p["title"] in r.text and p["excerpt"] in r.text


def test_an_empty_blog_still_renders(client):
    assert client.get("/blog").status_code == 200 and "New posts are on their way" in client.get("/blog").text


# ---------------------------------------------------------------- sitemap and robots
def test_the_sitemap_lists_the_blog_and_every_published_post_but_no_draft(client, db, published):
    db.add(BlogPost(slug="not-ready-yet", title="Draft", status="draft")); db.commit()
    r = client.get("/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(r.text)
    locs = [u.find("s:loc", ns).text for u in root.findall("s:url", ns)]
    assert locs[0] == f"{BLOG}/blog" and set(locs[1:]) == {f"{BLOG}/blog/{s}" for s in NEW_SLUGS} and "not-ready-yet" not in r.text
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d", u.find("s:lastmod", ns).text) for u in root.findall("s:url", ns)[1:])


def test_an_unpublished_post_leaves_the_sitemap_and_its_page(client, db, published):
    p = db.scalar(select(BlogPost).where(BlogPost.slug == NEW_SLUGS[0])); p.status = "draft"; db.commit()
    assert NEW_SLUGS[0] not in client.get("/sitemap.xml").text and "Post not found" in client.get(f"/blog/{NEW_SLUGS[0]}").text


def test_robots_points_to_the_sitemap_and_keeps_the_api_out_of_search(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "User-agent: *" in r.text and "Disallow: /api/" in r.text and f"Sitemap: {BLOG}/sitemap.xml" in r.text and "Disallow: /\n" not in r.text


# ---------------------------------------------------------------- nothing typed into a title or description can break a page
HOSTILE_TITLE = 'Cats & "Dogs" <b>x</b> </script><script>alert(1)</script>'
HOSTILE_META = 'Say "hi" & <script>alert(2)</script>'


def test_hostile_text_in_a_title_description_or_author_cannot_break_or_inject(client, db):
    db.add(BlogPost(slug="hostile", title=HOSTILE_TITLE, excerpt='Ex "cerpt" <i>', body_html="<p>Fine body.</p>", meta_description=HOSTILE_META,
                    author_name='A<script>alert(3)</script>', status="published", published_at=now_utc())); db.commit()
    raw = client.get("/blog/hostile").text
    h = parse(raw)
    assert h.title == f"{HOSTILE_TITLE} | BrandsLens" and h.meta["description"] == HOSTILE_META and h.h1s == [HOSTILE_TITLE]
    assert "<script>alert" not in raw and "<b>x</b>" not in raw
    assert raw.count("</script>") == 1 and raw.count('<script type="application/ld+json">') == 1, "only the structured-data script exists"
    assert json.loads(h.ld[0])["headline"] == HOSTILE_TITLE
    listing = client.get("/blog").text
    assert "<script>alert" not in listing and "<b>x</b>" not in listing and "&lt;b&gt;" in listing


def test_a_cover_image_address_is_escaped_and_used_in_the_structured_data(client, db):
    db.add(BlogPost(slug="with-cover", title="Has a cover", body_html="<p>x</p>", meta_description="d", cover_image='https://img.test/a.png?x=1&y="2"',
                    status="published", published_at=now_utc())); db.commit()
    raw = client.get("/blog/with-cover").text
    assert 'src="https://img.test/a.png?x=1&amp;y=&quot;2&quot;"' in raw and 'y="2"' not in raw.split("<body>")[1].split("</div>")[0]
    assert json.loads(parse(raw).ld[0])["image"] == 'https://img.test/a.png?x=1&y="2"'


def test_a_missing_post_still_answers_politely(client):
    r = client.get("/blog/nope")
    assert r.status_code == 200 and "Post not found" in r.text and parse(r.text).meta["robots"].startswith("index")
