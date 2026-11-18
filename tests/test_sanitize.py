from urllib.parse import parse_qs, urlsplit

import pytest

from readlater.sanitize import clean_article_html, html_to_text, summarise
from readlater.images import ImageSigner

HOSTILE = (
    '<p onclick="steal()" style="position:fixed">Hello</p><script>alert(1)</script>'
    '<iframe src="https://tracker.example.test/frame"></iframe>'
    '<img src="https://tracker.example.test/pixel.gif" onerror="steal()" srcset="https://tracker.example.test/2x.gif 2x">'
    '<a href="javascript:steal()">bad link</a><a href="https://good.example.test/">good link</a>'
    '<style>body{display:none}</style><form action="https://tracker.example.test/"><input name="q"></form>'
    '<object data="x.swf"></object><embed src="x.swf"><svg><script>alert(2)</script></svg>'
    '<video src="https://tracker.example.test/v.mp4"></video><audio src="https://tracker.example.test/a.mp3"></audio>'
    '<link rel="stylesheet" href="https://tracker.example.test/s.css"><meta http-equiv="refresh" content="0;url=https://x">'
    '<base href="https://tracker.example.test/"><table><tr><td colspan="2">cell</td></tr></table>'
)
SIGNER = ImageSigner(b"s" * 32)


def test_scripts_frames_forms_and_handlers_are_removed():
    cleaned = clean_article_html(HOSTILE, base_url="https://hostile.example.test/post").html
    lowered = cleaned.lower()
    for forbidden in ("<script", "alert(", "<iframe", "onclick", "onerror", "javascript:", "<style", "display:none",
                      "<form", "<input", "<object", "<embed", "<svg", "<video", "<audio", "<link", "<meta", "<base",
                      "style=", "srcset", "tracker.example.test"):
        assert forbidden not in lowered, forbidden
    assert "Hello" in cleaned
    assert '<a href="https://good.example.test/"' in cleaned
    assert '<td colspan="2">cell</td>' in cleaned


def test_links_open_in_a_new_tab_without_a_referrer():
    cleaned = clean_article_html('<a href="https://good.example.test/">x</a>', base_url="https://a.example.test/").html
    assert 'target="_blank"' in cleaned
    assert "noopener" in cleaned and "noreferrer" in cleaned


def test_relative_links_are_made_absolute_against_the_article():
    cleaned = clean_article_html('<a href="/wood">pine</a>', base_url="https://workshop.example.test/posts/bird-box").html
    assert 'href="https://workshop.example.test/wood"' in cleaned


def test_images_are_blocked_by_default_and_counted():
    article = clean_article_html('<p>a</p><img src="https://cdn.example.test/a.jpg"><img src="https://cdn.example.test/b.jpg">',
                                 base_url="https://a.example.test/")
    assert "<img" not in article.html
    assert "cdn.example.test" not in article.html
    assert article.hidden_images == 2


def test_images_go_through_the_signed_proxy_when_allowed():
    article = clean_article_html('<img src="/pics/a.jpg" alt="A" width="10" onerror="x()">', base_url="https://cdn.example.test/post",
                                 image_proxy=SIGNER.proxy_path)
    assert article.hidden_images == 0
    assert "cdn.example.test" not in article.html.split("?", 1)[0]
    src = article.html.split('src="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    parts = urlsplit(src)
    assert parts.path == "/img"
    query = parse_qs(parts.query)
    assert query["u"] == ["https://cdn.example.test/pics/a.jpg"]
    assert SIGNER.matches(query["u"][0], query["s"][0])
    assert 'alt="A"' in article.html
    assert "onerror" not in article.html


@pytest.mark.parametrize("source", ["javascript:alert(1)", "data:image/svg+xml,<svg onload=alert(1)>", "file:///etc/passwd", ""])
def test_odd_image_sources_are_dropped_even_when_images_are_allowed(source):
    article = clean_article_html(f'<img src="{source}">', base_url="https://a.example.test/", image_proxy=SIGNER.proxy_path)
    assert "/img?" not in article.html
    assert "javascript" not in article.html and "file:" not in article.html and "onload" not in article.html


def test_signatures_are_tied_to_the_url_and_the_secret():
    signature = SIGNER.sign("https://cdn.example.test/a.jpg")
    assert SIGNER.matches("https://cdn.example.test/a.jpg", signature)
    assert not SIGNER.matches("https://cdn.example.test/b.jpg", signature)
    assert not ImageSigner(b"t" * 32).matches("https://cdn.example.test/a.jpg", signature)
    assert not SIGNER.matches("https://cdn.example.test/a.jpg", "")
    assert not SIGNER.matches("https://cdn.example.test/a.jpg", None)


def test_text_extraction_drops_markup_and_script_text():
    text = html_to_text("<p>Fish &amp; chips</p><script>var secret = 1;</script><style>p{}</style>\n\n<p>tonight</p>")
    assert text == "Fish & chips tonight"


def test_summaries_are_cut_on_a_word():
    assert summarise("one two three four", 9) == "one two..."
    assert summarise("short", 9) == "short"
    assert summarise("", 9) == ""


@pytest.mark.parametrize("source", ["http://[bad/x.gif", "https://[::1/x.gif"])
def test_malformed_image_urls_never_reach_the_page(source):
    article = clean_article_html(f'<img src="{source}"><a href="{source}">x</a>', base_url="https://a.example.test/", image_proxy=SIGNER.proxy_path)
    assert "[" not in article.html
    blocked = clean_article_html(f'<img src="{source}">', base_url="https://a.example.test/")
    assert "<img" not in blocked.html
    assert blocked.hidden_images == 0


def test_inline_tags_do_not_add_spaces_but_blocks_do():
    assert html_to_text('<p>Cut from <a href="/wood">pine</a>.</p><p>Then <b>sand</b>it<br>well</p>') == "Cut from pine. Then sandit well"
