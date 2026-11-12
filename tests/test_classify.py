from datetime import date

import pytest

from app import classify


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("VERTIGO", "Vertigo"),
        ("THE ONLY LIVING PICKPOCKET IN NEW YORK", "The Only Living Pickpocket in New York"),
        ("Godzilla Minus Zero (Japanese with English Subtitles)", "Godzilla Minus Zero"),
        ("CAUGHT OUT THERE with Q&A and Panel Discussion", "Caught Out There"),
        ("BEING HEUMANN followed by post-screening discussion (OC)", "Being Heumann"),
        ("MET OPERA: SAMSON ET DALILA", "Samson Et Dalila"),
        ("Teresita's Dream: Cuba's Battle Against Alzheimer's - Followed by Panel Discussion",
         "Teresita's Dream: Cuba's Battle Against Alzheimer's"),
        ("Jaws 50th Anniversary", "Jaws"),
        ("Avatar 3D", "Avatar"),
        ("ROCKY II", "Rocky II"),
    ],
)
def test_clean_title(raw, clean):
    assert classify.clean_title(raw) == clean


def test_clean_title_keeps_colon_subtitles():
    assert classify.clean_title("Dune: Part Three") == "Dune: Part Three"


def test_normalize_key_ignores_case_articles_and_punctuation():
    assert classify.normalize_key("THE BIRDS") == classify.normalize_key("Birds")
    assert classify.normalize_key("Dune: Part Three") == classify.normalize_key("DUNE PART THREE")


@pytest.mark.parametrize(
    ("raw", "tags", "flag", "expected"),
    [
        ("Wild Horse Nine", [], False, False),
        ("CAUGHT OUT THERE with Q&A and Panel Discussion", [], False, True),
        ("MET OPERA: OTELLO", [], False, True),
        ("Jaws 50th Anniversary", [], False, True),
        ("Some Film", ["Fathom Events"], False, True),
        ("Some Film", [], True, True),
        ("COLIN & BRAD LIVE PERFORMANCE", [], False, True),
    ],
)
def test_is_event(raw, tags, flag, expected):
    assert classify.is_event(raw, tags, flag) is expected


def test_formats():
    tags = ["Luxury Lounger XD Japanese Spoken with English Subtitles XD D-Box", "Cinemark XD"]
    assert classify.formats("Godzilla Minus Zero (Japanese with English Subtitles)", tags) == ["XD", "Subtitled", "D-BOX"]
    assert classify.formats("Being Heumann (OC)", []) == ["Open captions"]
    assert classify.formats("Plain Film", ["Standard Format Luxury Lounger"]) == []


def test_rerelease_and_advance():
    show = date(2026, 11, 14)
    assert classify.is_rerelease(date(1958, 7, 21), show, 9)
    assert not classify.is_rerelease(date(2026, 10, 23), show, 9)
    assert classify.is_rerelease(date(2025, 12, 1), show, 9)
    assert not classify.is_rerelease(date(2025, 12, 1), show, 60)
    assert not classify.is_rerelease(None, show, 9)
    assert classify.is_advance(date(2026, 12, 18), show)
    assert not classify.is_advance(date(2026, 11, 6), show)
