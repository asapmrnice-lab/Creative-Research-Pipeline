import pytest

from research_pipeline.filtering import KeywordFilter, KeywordFilterConfig

KEYWORDS = ("креатив", "креативчик", "крео", "креос", "кейс")


def make_filter(**overrides) -> KeywordFilter:
    config = KeywordFilterConfig(keywords=KEYWORDS, **overrides)
    return KeywordFilter(config)


@pytest.mark.parametrize(
    "text",
    [
        "нужен креатив на завтра",
        "КРЕАТИВ в шапке",  # case-insensitive
        "залил крео в кабинет",
        "новые креосы приехали",  # plural
        "работаем с креативом",  # instrumental
        "в креативах есть оффер",  # prepositional plural
        "(креатив)",  # punctuation boundary
        "креатив,крео",  # no spaces
        "залил креативчик",  # diminutive, own keyword
        "пачка креативчиков",  # diminutive, plural genitive
        "с креативчиками работаем",
        "кейс по фейсбуку",
        "разбор кейсов за июль",
        "в кейсе есть цифры",
    ],
)
def test_matches(text):
    assert make_filter().matches(text)


def test_diminutive_is_its_own_keyword():
    """"креативчик" must not be reachable from the "креатив" stem."""
    kf = KeywordFilter(KeywordFilterConfig(keywords=("креатив",)))
    assert not kf.matches("залил креативчик")


@pytest.mark.parametrize(
    "text",
    [
        "креативный подход",  # adjective, not the noun
        "прекреатив",  # prefixed
        "рекреация",
        "creative in english",
        "кейсбук",  # not the noun "кейс"
        "кейсинг",
        "",
        None,
    ],
)
def test_does_not_match(text):
    assert not make_filter().matches(text)


# --------------------------------------------------------------------------
# Strict list adherence: a compound is a different word, not the keyword.
#
# Both halves matter. Glued and hyphenated compounds must be rejected the
# same way -- rejecting "антикейс" while admitting "анти-кейс" would be an
# arbitrary hole. Punctuation that leaves the word standing alone must still
# match, or hashtags and quoted words would be lost.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "антикейс на 1 500 000 рублей",  # prefix, glued
        "анти-кейс на 1 500 000 рублей",  # same word, hyphenated
        "псевдокреатив",
        "суперкейс",
        "мега-креатив",
        "видео-креативов",  # relevant, but not a listed keyword
        "кейса-мануала",
        "креатив-менеджер",  # keyword as the first half
        "крео-тест",
        "креатив2",  # digits are not an inflection
        "кейс_архив",  # underscore is not a separator
    ],
)
def test_compounds_are_not_the_keyword(text):
    assert not make_filter().matches(text)


def test_a_compound_is_collected_only_by_naming_it_in_the_list():
    """The escape hatch: the list stays the single source of truth."""
    assert not make_filter().matches("тестирование видео-креативов")
    named = KeywordFilter(KeywordFilterConfig(keywords=("видео-креатив",)))
    assert named.matches("тестирование видео-креативов")


@pytest.mark.parametrize(
    "text",
    [
        "#креатив",  # hashtag
        '"кейс"',
        "«кейс» недели",
        "кейс/крео",
        "крео)",
        "кейс - это важно",  # spaced dash, not a compound
        "кейс:",
        "смотри — креатив",
    ],
)
def test_punctuation_still_leaves_the_word_standing_alone(text):
    assert make_filter().matches(text)


def test_strict_whole_word_rejects_inflections():
    kf = make_filter(allow_russian_inflections=False)
    assert kf.matches("креатив готов")
    assert not kf.matches("креативы готовы")


def test_substring_mode_is_looser():
    kf = make_filter(match="substring")
    assert kf.matches("креативный подход")


def test_find_reports_keyword_and_form():
    hits = make_filter().find("два креатива и одно крео")
    assert [(h.keyword, h.matched_text) for h in hits] == [
        ("креатив", "креатива"),
        ("крео", "крео"),
    ]


def test_matched_keywords_dedupes():
    kf = make_filter()
    assert kf.matched_keywords("креатив, креативы, крео") == ["креатив", "крео"]


def test_from_env_parses_config():
    config = KeywordFilterConfig.from_env(
        {
            "INGEST_KEYWORDS": " креатив , креативчик , крео ,, креос , кейс ",
            "INGEST_KEYWORD_MATCH": "whole-word",
            "INGEST_KEYWORD_INFLECTIONS": "false",
        }
    )
    assert config.keywords == KEYWORDS
    assert config.allow_russian_inflections is False


def test_from_env_rejects_empty_keywords():
    with pytest.raises(ValueError, match="INGEST_KEYWORDS is empty"):
        KeywordFilterConfig.from_env({"INGEST_KEYWORDS": "  "})


def test_from_env_rejects_unknown_match_mode():
    with pytest.raises(ValueError, match="INGEST_KEYWORD_MATCH"):
        KeywordFilterConfig.from_env(
            {"INGEST_KEYWORDS": "крео", "INGEST_KEYWORD_MATCH": "fuzzy"}
        )
