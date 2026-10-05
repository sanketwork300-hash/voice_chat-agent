"""Voice helpers: text shaped for speech, spoken names, and turn timing."""

from backend.agent.extract import spoken_key
from backend.voice.pipeline import TurnTiming, spoken


def test_spoken_drops_markup_citations_and_screen_identifiers():
    text = ("Your instance demo-web-01 (i-0abc123def) has **security group sg-0a1b2c3d** blocking TCP port 22 "
            "from 203.0.113.10/32 [1]. See the docs [2, 3].")
    assert spoken(text) == ("Your instance demo web 01 has the instance's security group blocking port 22 "
                            "from 203.0.113.10. See the docs.")


def test_spoken_separates_name_parts_but_not_numbers():
    assert spoken("Let me check demo-report-generator.") == "Let me check demo report generator."
    assert spoken("The IP is 203.0.113.10 and the port is 22.") == "The IP is 203.0.113.10 and the port is 22."


def test_spoken_leaves_plain_text_alone():
    assert spoken("The maximum timeout is 15 minutes.") == "The maximum timeout is 15 minutes."


def test_spoken_key_matches_names_as_they_are_said():
    assert spoken_key("demo-web-01") == spoken_key("Demo Web 01") == spoken_key("demoweb01")
    assert spoken_key("web-server-1") == spoken_key("web server one")
    assert spoken_key("demo-web-01") != spoken_key("demo-web-02")


def test_turn_timing_reports_the_stages_it_saw():
    timing = TurnTiming()
    timing.user_stopped, timing.transcript, timing.first_token, timing.first_audio = 10.0, 11.5, 14.0, 14.4
    assert timing.stages(20.0) == {"stt": 1.5, "first_token": 2.5, "first_audio": 2.9, "total": 10.0}

    partial = TurnTiming()
    partial.user_stopped = 10.0
    assert partial.stages(12.0) == {"total": 2.0}
    assert TurnTiming().stages(12.0) == {}
