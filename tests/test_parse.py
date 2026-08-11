"""Table-driven grammar tests. No filesystem, no clock - `today` is injected."""

import datetime as dt

import pytest

from tenx.models import Skill
from tenx.parse import Command, ParseError, QuickAdd, parse, parse_duration

TODAY = dt.date(2026, 8, 9)  # a Sunday

SKILLS = [
    Skill("ml", "machine learning", "2026-01-01", aliases=("mlr",)),
    Skill("lc", "leetcode", "2026-01-01"),
    Skill("run", "running", "2026-01-01"),
    Skill("poker", "poker", "2026-01-01"),
    Skill("piano", "piano", "2026-01-01"),
    Skill("go", "go", "2026-01-01"),
    Skill("golang", "golang", "2026-01-01"),
    Skill("old", "old thing", "2026-01-01", archived=True),
]


def add(text):
    return parse(text, SKILLS, TODAY)


# --- durations --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,minutes",
    [
        ("ml 1h30", 90),
        ("ml 90", 90),
        ("ml 90m", 90),
        ("ml 1.5h", 90),
        ("ml 1h30m", 90),
        ("ml 1h", 60),
        ("ml 2h15", 135),
        ("ml 24h", 1440),
        ("ml 1", 1),
    ],
)
def test_duration_forms(text, minutes):
    result = add(text)
    assert isinstance(result, QuickAdd), result
    assert result.minutes == minutes


@pytest.mark.parametrize(
    "text", ["ml 0m", "ml 0", "ml -5m", "ml 25h", "ml 1h3o", "ml abc", "ml 1441", "ml 1h90", "ml 1h60"]
)
def test_duration_rejects(text):
    assert isinstance(add(text), ParseError)


def test_fractional_hours_with_explicit_minutes_is_ambiguous():
    assert parse_duration("1.5h30")[0] is None


def test_hm_minutes_must_be_under_60():
    # "1h90" used to silently parse as 150 minutes (60 + 90) - it must be
    # rejected instead, the same principle already applied to 1.5h30.
    minutes, error = parse_duration("1h90")
    assert minutes is None and error is not None


def test_empty_input():
    assert isinstance(add(""), ParseError)
    assert isinstance(add("   "), ParseError)


def test_skill_without_duration():
    result = add("ml")
    assert isinstance(result, ParseError)
    assert "1h30" in result.message


# --- dates ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ml 1h", TODAY),
        ("ml 1h today", TODAY),
        ("ml 1h yesterday", dt.date(2026, 8, 8)),
        ("ml 1h mon", dt.date(2026, 8, 3)),
        ("ml 1h sun", TODAY),  # today, when the weekday matches
        ("ml 1h friday", dt.date(2026, 8, 7)),
        ("ml 1h -1", dt.date(2026, 8, 8)),
        ("ml 1h -2", dt.date(2026, 8, 7)),
        ("ml 1h 2026-08-05", dt.date(2026, 8, 5)),
        ("ml 1h 2024-02-29", dt.date(2024, 2, 29)),
    ],
)
def test_date_forms(text, expected):
    result = add(text)
    assert isinstance(result, QuickAdd), result
    assert result.date == expected


@pytest.mark.parametrize("text", ["ml 1h 2027-01-01", "ml 1h 2026-13-01"])
def test_date_rejects(text):
    # Only the unambiguous ISO form hard-errors on a bad date. It's an
    # explicit format the user chose on purpose, so a mistake is worth
    # surfacing rather than silently becoming a note.
    assert isinstance(add(text), ParseError)


@pytest.mark.parametrize("text", ["ml 1h 2/30", "ml 1h 13/1", "ml 1h 8/5"])
def test_slash_tokens_are_not_dates_at_all(text):
    # There's no M/D form - it's the one date shape that collides with note
    # content ("3/4 squats", "12/8 deadlift"), so any slash token, valid
    # calendar date or not, is just note text like any other unrecognized word.
    result = add(text)
    assert isinstance(result, QuickAdd)
    assert result.date == TODAY
    assert result.note == text.split(maxsplit=2)[2]


def test_iso_lookalikes_are_not_dates():
    # date.fromisoformat also accepts 20260805 and 2026-W32-1; those must fall
    # through to the note rather than silently becoming a date.
    result = add("ml 1h 20260805 compact")
    assert isinstance(result, QuickAdd)
    assert result.date == TODAY
    assert result.note == "20260805 compact"


# --- notes ------------------------------------------------------------------


def test_note_starting_with_a_digit_is_not_eaten_as_a_date():
    result = add("ml 1h 5 papers read")
    assert isinstance(result, QuickAdd)
    assert result.date == TODAY
    assert result.note == "5 papers read"


def test_note_after_a_date():
    # An unambiguous ISO date still backdates correctly even with a note
    # trailing it.
    result = add("poker 2h 2026-08-05 deep stack notes")
    assert isinstance(result, QuickAdd)
    assert result.skill == "poker"
    assert result.minutes == 120
    assert result.date == dt.date(2026, 8, 5)
    assert result.note == "deep stack notes"


def test_md_date_with_trailing_note_stays_a_note():
    # "12/8" here reads exactly like fitness set/rep notation. There's no
    # M/D date form at all now, so this is never mistaken for a date -
    # regardless of whether a note follows it (see test_slash_tokens_are_not_dates_at_all
    # for the sole-token case).
    result = add("run 1h 12/8 hill sprints")
    assert isinstance(result, QuickAdd)
    assert result.date == TODAY
    assert result.note == "12/8 hill sprints"


def test_colon_inside_a_note_is_not_a_command():
    result = add("ml 1h todo: rerun the sweep")
    assert isinstance(result, QuickAdd)
    assert result.note == "todo: rerun the sweep"


def test_no_note():
    result = add("ml 1h30")
    assert isinstance(result, QuickAdd)
    assert result.note == ""


# --- skill resolution -------------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [
        ("ml", "ml"),
        ("mlr", "ml"),  # exact alias
        ("lee", "lc"),  # prefix of the display name
        ("pok", "poker"),
        ("go", "go"),  # exact id beats the "golang" prefix
        ("gol", "golang"),
        ("old", "old"),  # archived skills stay resolvable for backfill
        ("ML", "ml"),  # case insensitive
    ],
)
def test_skill_resolution(token, expected):
    result = add(f"{token} 1h")
    assert isinstance(result, QuickAdd), result
    assert result.skill == expected


def test_ambiguous_prefix_lists_candidates():
    result = add("p 1h")
    assert isinstance(result, ParseError)
    assert "piano" in result.message and "poker" in result.message


def test_unknown_skill_suggests_new():
    result = add("xyz 1h")
    assert isinstance(result, ParseError)
    assert ":new xyz" in result.message


# --- commands ---------------------------------------------------------------


def test_command_parsing():
    result = add(":new ml machine learning")
    assert isinstance(result, Command)
    assert result.name == "new"
    assert result.args == ("ml", "machine", "learning")
    assert result.rest == "ml machine learning"


@pytest.mark.parametrize(
    "text,name",
    [(":undo", "undo"), (":sync", "sync"), (":q", "q"), (":filter off", "filter"), (":year 2024", "year")],
)
def test_simple_commands(text, name):
    result = add(text)
    assert isinstance(result, Command)
    assert result.name == name


@pytest.mark.parametrize(
    "text",
    [
        ":nope",  # unknown command
        ":",  # no command at all
        ":new",  # missing args
        ":rename ml",  # missing args
        ":archive",  # missing args
        ":archive a b",  # extra args
        ":undo now",  # extra args
        ":year 26",  # not a 4-digit year
        ":export json",  # only csv
        ":edit 3",  # missing duration
    ],
)
def test_command_rejects(text):
    assert isinstance(add(text), ParseError)


def test_detail_shortcut():
    result = add("d ml")
    assert isinstance(result, Command)
    assert result.name == "detail"
    assert result.args == ("ml",)


def test_detail_shortcut_yields_to_a_real_duration():
    skills = SKILLS + [Skill("d", "drawing", "2026-01-01")]
    result = parse("d 30m", skills, TODAY)
    assert isinstance(result, QuickAdd)
    assert result.skill == "d"


def test_parse_is_pure():
    """Same inputs, same output - nothing here reads a clock or urandom."""
    assert add("ml 1h30 sweep") == add("ml 1h30 sweep")


# --- custom metrics ---------------------------------------------------------

RUN = Skill("run", "running", "2026-01-01", metrics=("distance", "shoes"))
METRIC_SKILLS = [RUN, Skill("ml", "machine learning", "2026-01-01")]


def metric_add(text):
    return parse(text, METRIC_SKILLS, TODAY)


def test_a_declared_metric_is_pulled_out_of_the_note():
    result = metric_add("run 45m distance=8.2")
    assert isinstance(result, QuickAdd)
    assert result.metrics == {"distance": 8.2}
    assert result.note == ""


def test_metrics_can_sit_anywhere_after_the_duration():
    result = metric_add("run 45m easy distance=8.2 shoes=vaporfly")
    assert isinstance(result, QuickAdd)
    assert result.metrics == {"distance": 8.2, "shoes": "vaporfly"}
    assert result.note == "easy"


def test_a_metric_does_not_shadow_the_date():
    """Metrics come out first, so what remains still starts at position 3."""
    result = metric_add("run 45m distance=8.2 yesterday easy pace")
    assert isinstance(result, QuickAdd)
    assert result.date == dt.date(2026, 8, 8)
    assert result.metrics == {"distance": 8.2} and result.note == "easy pace"


def test_an_undeclared_key_stays_in_the_note():
    """ml has no `loss` metric, so this is just text - declaring is what makes
    a key a metric, which keeps notes with equals signs intact."""
    result = metric_add("ml 1h loss=0.23 after 3 epochs")
    assert isinstance(result, QuickAdd)
    assert result.metrics == {} and result.note == "loss=0.23 after 3 epochs"


def test_a_note_with_an_equals_sign_is_left_alone():
    result = metric_add("run 30m todo: check a=b")
    assert isinstance(result, QuickAdd)
    assert result.metrics == {} and result.note == "todo: check a=b"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("run 45m distance=8", 8),
        ("run 45m distance=8.2", 8.2),
        ("run 45m shoes=vaporfly", "vaporfly"),
        ("run 45m distance=-3", -3),
    ],
)
def test_metric_values_keep_their_type(text, expected):
    result = metric_add(text)
    assert isinstance(result, QuickAdd)
    assert list(result.metrics.values()) == [expected]


def test_a_declared_key_with_an_empty_value_is_an_error():
    # distance is declared on `run`, so a malformed value is a mistake worth
    # surfacing - not a silent fallthrough to note text (unlike an
    # undeclared key, which stays a note; see test_an_undeclared_key_stays_in_the_note).
    result = metric_add("run 45m distance=")
    assert isinstance(result, ParseError)
    assert "distance" in result.message


def test_metric_command():
    assert parse(":metric run distance", METRIC_SKILLS, TODAY) == Command(
        name="metric", args=("run", "distance"), rest="run distance"
    )
    assert isinstance(parse(":metric run -distance", METRIC_SKILLS, TODAY), Command)


@pytest.mark.parametrize("text", [":metric run", ":metric run BAD KEY", ":metric run 9lives", ":metric"])
def test_metric_command_rejects(text):
    assert isinstance(parse(text, METRIC_SKILLS, TODAY), ParseError)
