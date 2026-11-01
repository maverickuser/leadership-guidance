import random

from newsletter_archive.search.boilerplate import EMPTY, find_boilerplate, paragraph_key
from newsletter_archive.search.models import Section

from .textgen import paragraph

INTRO = (
    "Welcome to the example letter, I'm a director of engineering who has spent many years building "
    "teams and growing leaders at large companies. Each week I write about careers, hiring, promotions, "
    "feedback and the habits that help managers and engineers do their best work. Thanks for being "
    "here, and please share it with a friend who might enjoy it too."
)
SIGNUP = "Free members can read part of each article, and paid members can read every word of it."


def _posts(n, extra_first=None, start=0, seed=0):
    rng = random.Random(seed + start)
    posts = {}
    for i in range(start, start + n):
        paras = ([extra_first(i)] if extra_first else []) + [paragraph(rng, 200), paragraph(rng, 150)]
        posts[f"post-{i:03d}"] = [Section(None, "\n\n".join(paras))]
    return posts


# spec: search-indexing / Boilerplate filter / Repeated intro dropped
def test_repeated_intro_dropped_everywhere():
    posts = _posts(70, lambda i: INTRO)
    bp = find_boilerplate(posts)
    assert len(bp.clusters) == 1 and bp.clusters[0].post_count == 70
    assert bp.clusters[0].sample.startswith("Welcome to the example")
    for sections in posts.values():
        result = bp.clean(sections)
        assert result.removed_paragraphs == 1
        assert INTRO not in result.sections[0].text
        assert 0 < result.loss_fraction < 0.2


# spec: search-indexing / Boilerplate filter / Reworded copy dropped
def test_reworded_copies_join_the_cluster():
    reworded = INTRO.replace("a director of engineering", "a VP of engineering")
    posts = _posts(40, lambda i: INTRO)
    posts.update(_posts(3, lambda i: reworded, start=40))
    bp = find_boilerplate(posts)
    assert [c.post_count for c in bp.clusters] == [43]
    assert bp.is_boilerplate(reworded) and bp.is_boilerplate(INTRO.upper() + "!!")


# spec: search-indexing / Boilerplate filter / Shared quote kept
def test_paragraph_in_few_posts_is_kept():
    quote = "The best way to predict the future is to create it, as the old saying goes."
    bp = find_boilerplate(_posts(3, lambda i: quote))
    assert bp.clusters == () and not bp.is_boilerplate(quote)


# spec: search-indexing / Boilerplate filter / New boilerplate appears
def test_new_boilerplate_is_found_once_it_reaches_the_threshold():
    signoff = "Thanks for reading this week, reply to this email with any questions you have for me."
    four = _posts(4, lambda i: signoff)
    assert find_boilerplate(four).clusters == ()
    five = {**four, **_posts(1, lambda i: signoff, start=4)}
    bp = find_boilerplate(five)
    assert bp.is_boilerplate(signoff)
    assert all(bp.clean(s).removed_paragraphs == 1 for s in five.values())


def test_short_lines_and_different_paragraphs_are_not_grouped():
    posts = _posts(10, lambda i: "Thanks for reading!")
    other = "Paid members can read part of each article, and every member gets the weekly summary."
    posts.update(_posts(4, lambda i: other, start=10))
    posts.update(_posts(5, lambda i: SIGNUP, start=14))
    bp = find_boilerplate(posts)
    assert [c.post_count for c in bp.clusters] == [5]  # SIGNUP only; `other` is too different
    assert not bp.is_boilerplate("Thanks for reading!") and not bp.is_boilerplate(other)


def test_order_independent_and_sections_emptied_are_dropped():
    posts = _posts(6, lambda i: INTRO)
    shuffled = dict(reversed(list(posts.items())))
    assert find_boilerplate(posts) == find_boilerplate(shuffled)
    bp = find_boilerplate(posts)
    result = bp.clean([Section("Intro only", INTRO), Section("Body", "Real text stays.")])
    assert [s.heading for s in result.sections] == ["Body"]
    assert result.total_words > result.removed_words > 0


def test_empty_inputs():
    assert find_boilerplate({}) is EMPTY
    assert find_boilerplate({"a": [Section(None, "short")]}) is EMPTY
    assert EMPTY.clean([]).loss_fraction == 0.0
    assert paragraph_key("A  b, c!") == paragraph_key("a b c")
