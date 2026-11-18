from datetime import datetime, timezone

import pytest
from conftest import fixture_bytes

from readlater.extract import ExtractError, decode_page, extract_article, is_html_type, reading_minutes

ARTICLE_URL = "https://workshop.example.test/posts/bird-box"


def workshop_article():
    return extract_article(fixture_bytes("article.html").decode("utf-8"), ARTICLE_URL)


def test_metadata_comes_from_the_page_tags():
    article = workshop_article()
    assert article.title == "How to build a bird box"
    assert article.site_name == "The Workshop Blog"
    assert article.author == "Sam Carpenter"
    assert article.language == "en-GB"
    assert article.excerpt == "A weekend project with one plank of pine."
    assert article.published_at == datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc).timestamp()


def test_the_readable_body_keeps_the_story_and_drops_the_page_around_it():
    article = workshop_article()
    assert "simplest woodworking projects" in article.text
    assert "Clean it out each autumn" in article.text
    for clutter in ("Popular posts", "Sidebar link", "cheap saws", "Comment spam", "Footer text", "tracking", "About us"):
        assert clutter not in article.text, clutter
    assert "<script" not in article.content_html.lower()
    assert "tracker.example.test" not in article.content_html


def test_links_and_images_in_the_body_are_made_absolute():
    html = workshop_article().content_html
    assert 'href="https://workshop.example.test/guides/hole-sizes"' in html
    assert 'src="https://workshop.example.test/images/bird-box.jpg"' in html


def test_word_count_and_reading_time():
    article = workshop_article()
    assert 200 <= article.word_count <= 240
    assert article.reading_minutes == 1


@pytest.mark.parametrize("words, minutes", [(0, 0), (1, 1), (230, 1), (231, 2), (2300, 10), (2301, 11)])
def test_reading_minutes_round_up_at_230_words_a_minute(words, minutes):
    assert reading_minutes(words) == minutes


def test_a_page_without_metadata_falls_back_to_the_title_tag_and_the_host():
    article = extract_article(fixture_bytes("bare.html").decode(), "https://www.plain.example.test/page")
    assert article.title == "Plain page title"
    assert article.site_name == "plain.example.test"
    assert article.author == ""
    assert article.published_at is None
    assert article.excerpt.startswith("Just one paragraph of text")
    assert article.word_count > 10


def test_a_page_without_any_title_is_named_after_its_address():
    article = extract_article("<html><body><p>Words and more words on a page that has no title tag.</p></body></html>",
                              "https://plain.example.test/notes/today")
    assert article.title == "plain.example.test/notes/today"


def test_odd_metadata_values_are_ignored():
    page = ('<html><head><meta property="article:published_time" content="last tuesday">'
            '<meta name="author" content="https://social.example.test/@sam"><title>T</title></head>'
            '<body><p>Enough words on this page for the reader to keep a body for the test.</p></body></html>')
    article = extract_article(page, "https://plain.example.test/")
    assert article.published_at is None
    assert article.author == ""


def test_long_titles_are_cut():
    page = f"<html><head><title>{'word ' * 200}</title></head><body><p>Short body text that is long enough.</p></body></html>"
    assert len(extract_article(page, "https://plain.example.test/").title) <= 300


@pytest.mark.parametrize("page", ["", "   ", "<html></html>"])
def test_empty_pages_cannot_be_read(page):
    with pytest.raises(ExtractError):
        extract_article(page, "https://plain.example.test/")


def test_the_charset_from_the_page_is_used_when_the_server_gives_none():
    text = decode_page(fixture_bytes("latin1.html"), "text/html")
    assert "crème brûlée" in text
    assert "café" in extract_article(text, "https://cafe.example.test/").text


def test_the_server_charset_wins_over_the_page():
    body = '<html><head><meta charset="iso-8859-1"></head><body>café</body></html>'.encode("utf-8")
    assert "café" in decode_page(body, "text/html; charset=utf-8")


@pytest.mark.parametrize("header", ["text/html; charset=nonsense-9", "text/html"])
def test_unknown_or_missing_charsets_fall_back_to_utf8(header):
    assert "naïve" in decode_page("<p>naïve</p>".encode("utf-8"), header)


@pytest.mark.parametrize("content_type, expected", [
    ("text/html", True), ("application/xhtml+xml", True), ("", True),
    ("application/pdf", False), ("image/png", False), ("application/json", False),
])
def test_only_html_answers_are_read_as_articles(content_type, expected):
    assert is_html_type(content_type) is expected


def test_a_leading_heading_that_repeats_the_title_is_dropped():
    article = workshop_article()
    assert "<h1>How to build a bird box</h1>" not in article.content_html
    assert article.text.startswith("Building a bird box")


def test_a_leading_heading_with_other_words_is_kept():
    page = ("<html><head><title>Garden notes</title></head><body><article><h2>Before you start</h2>"
            "<p>Words enough for the reader to keep this paragraph as the body of the article, and a few more.</p></article></body></html>")
    article = extract_article(page, "https://plain.example.test/")
    assert "Before you start" in article.text


def test_a_page_that_is_only_its_title_keeps_the_heading():
    page = '<html><head><title>Only</title></head><body><div><h1>Only</h1><img src="/a.png"></div></body></html>'
    article = extract_article(page, "https://plain.example.test/")
    assert article.text == "Only"


@pytest.mark.parametrize("moment", ["9999-12-31T23:59:59-12:00", "0001-01-01T00:00:00+12:00", "9999-12-31T23:59:59-23:59"])
def test_published_times_outside_the_showable_years_are_dropped(moment):
    page = (f'<html><head><title>T</title><meta property="article:published_time" content="{moment}"></head>'
            "<body><article><p>" + "Words enough to make an article body. " * 20 + "</p></article></body></html>")
    assert extract_article(page, ARTICLE_URL).published_at is None


NOT_TEXT_CHARSETS = ["hex", "rot13", "zlib", "base64", "idna", "undefined", "punycode", "uu", "bz2", "quopri"]


@pytest.mark.parametrize("charset", NOT_TEXT_CHARSETS)
def test_charsets_that_are_not_text_encodings_fall_back_to_utf8(charset):
    body = "<p>naïve</p>".encode("utf-8")
    assert "naïve" in decode_page(body, f"text/html; charset={charset}")
    assert "naïve" in decode_page(f'<meta charset="{charset}">'.encode() + body, "text/html")
