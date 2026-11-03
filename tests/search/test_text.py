from hypothesis import given
from hypothesis import strategies as st

from newsletter_archive.search.sentences import split_sentences
from newsletter_archive.search.text import fold, normalize_for_matching, paragraphs, squash, word_count


def test_fold_removes_accents():
    assert fold("Résumé naïve café Øresund straße Łódź") == "Resume naive cafe Oresund strasse Lodz"
    assert fold("plain ascii") == "plain ascii"


def test_paragraphs_and_normalisation():
    assert paragraphs("One  two.\n\n\n  Three\nfour. \n \n") == ["One two.", "Three four."]
    assert normalize_for_matching("Welcome, to the Café!  I'm here.") == "welcome to the cafe i m here"
    assert word_count(" a b  c ") == 3 and squash(" a \n b ") == "a b"


def test_sentence_boundaries():
    text = (
        'Dr. Smith joined in 2.5 years. He led U.S. teams. Really? Yes! "Quoted." '
        "Next one... Then J. K. Rowling wrote, e.g. this. (Aside.) 3 more"
    )
    assert split_sentences(text) == [
        "Dr. Smith joined in 2.5 years.",
        "He led U.S. teams.",
        "Really?",
        "Yes!",
        '"Quoted."',
        "Next one...",
        "Then J. K. Rowling wrote, e.g. this.",
        "(Aside.)",
        "3 more",
    ]


def test_ordinary_words_end_sentences():
    text = "Thanks for joining us. Next week we start. The answer is no. We move on. Look at the Sun. Done."
    assert len(split_sentences(text)) == 6


def test_abbreviation_rules():
    assert split_sentences("See No. 5 today. Ask Dr. Smith. Acme Inc. Today.") == [
        "See No. 5 today.",
        "Ask Dr. Smith.",
        "Acme Inc. Today.",
    ]
    assert split_sentences("Apples vs. Oranges. Mar. 3 was cold.") == [
        "Apples vs. Oranges.",
        "Mar. 3 was cold.",
    ]
    assert split_sentences("I said I. Then we left. Grade A. Next.") == [
        "I said I.",
        "Then we left.",
        "Grade A.",
        "Next.",
    ]
    assert split_sentences(". Leading period") == [".", "Leading period"]


def test_no_split_before_lowercase_or_inside_numbers():
    assert split_sentences("It costs $3.50 per unit. see you") == ["It costs $3.50 per unit. see you"]
    assert split_sentences("") == [] and split_sentences("   ") == []
    assert split_sentences("Ends here.") == ["Ends here."]


_TEXT = st.text(alphabet=st.sampled_from(list("abc ABC.!?\"'()2\n é")), max_size=200)


@given(_TEXT)
def test_split_rejoins_to_the_squashed_paragraph(text):
    parts = split_sentences(text)
    assert " ".join(parts) == squash(text)
    assert all(p == p.strip() and p for p in parts)
