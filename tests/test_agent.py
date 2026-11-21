import copy
import datetime
import time

import pandas as pd
import pytest

from agent import (
    LocalClient, SYSTEM_PROMPT, TIME_LIMIT_ANSWER, _from_ollama_response, _strip_thinking, _to_ollama_messages,
    _to_ollama_tools, run_agent, run_agent_with_trace,
)
from tools import TOOL_SPECS
from storage import connect, latest_snapshot, save_snapshot
from tools import Toolbox
from universe import load_groups


NAMES = {"AAA": "Alpha", "BBB": "Beta", "CCC": "Gamma", "DDD": "Delta", "EEE": "Epsilon"}


def make_returns():
    return pd.DataFrame(
        {
            "close": [100.0, 50.0, 20.0, 10.0, 5.0],
            "day_pct": [2.0, -1.5, 25.0, -30.0, 0.5],
            "week_pct": [4.0, -3.0, 10.0, -12.0, 1.0],
            "month_pct": [10.0, -8.0, 20.0, -40.0, 2.0],
            "market_cap": [3e12, 5e11, 4e10, 1e12, 2e11],
        },
        index=pd.Index(["AAA", "BBB", "CCC", "DDD", "EEE"], name="ticker"),
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    save_snapshot(c, datetime.date(2026, 11, 2), make_returns())
    return c


@pytest.fixture
def toolbox(conn):
    return Toolbox(conn, NAMES, outlier_threshold=20.0)


# ---------- storage ----------

def test_latest_snapshot_picks_newest_day(tmp_path):
    c = connect(tmp_path / "t.db")
    old = make_returns()
    old["close"] = 1.0
    save_snapshot(c, datetime.date(2026, 10, 30), old)
    save_snapshot(c, datetime.date(2026, 11, 2), make_returns())
    day, df = latest_snapshot(c)
    assert day == "2026-11-02"
    assert df.loc["AAA", "close"] == 100.0


def test_latest_snapshot_empty(tmp_path):
    day, df = latest_snapshot(connect(tmp_path / "t.db"))
    assert day is None and df.empty


# ---------- tools ----------

def test_get_movers_losers_sorted_and_labeled(toolbox):
    res = toolbox.get_movers("week", "losers", 2)
    assert res["as_of"] == "2026-11-02"
    assert [s["ticker"] for s in res["stocks"]] == ["DDD", "BBB"]
    assert res["stocks"][0]["week_pct"] == -12.0
    assert res["stocks"][0]["name"] == "Delta"


def test_get_movers_rejects_bad_input(toolbox):
    with pytest.raises(ValueError):
        toolbox.get_movers("year", "gainers")
    with pytest.raises(ValueError):
        toolbox.get_movers("day", "biggest")


def test_get_movers_clamps_n(toolbox):
    assert len(toolbox.get_movers("day", "gainers", 999)["stocks"]) == 5


def test_get_top_companies_dedupes_share_classes(conn):
    tb = Toolbox(conn, NAMES, same_company_groups=[("DDD", "BBB")])
    tickers = [c["ticker"] for c in tb.get_top_companies(3)["companies"]]
    assert tickers == ["AAA", "DDD", "EEE"]  # BBB dropped because DDD is bigger
    assert tb.get_top_companies(1)["companies"][0]["market_cap_billions"] == 3000.0


def test_get_ticker_history(toolbox):
    res = toolbox.get_ticker_history("aaa", 30)
    assert res["ticker"] == "AAA" and len(res["rows"]) == 1
    assert res["rows"][0]["day_pct"] == 2.0
    with pytest.raises(ValueError):
        toolbox.get_ticker_history("ZZZ")


def test_get_outliers_biggest_first(toolbox):
    res = toolbox.get_outliers()
    assert [s["ticker"] for s in res["stocks"]] == ["DDD", "CCC"]
    assert toolbox.get_outliers(threshold=50)["stocks"] == []


def test_toolbox_without_data_raises_clear_error(tmp_path):
    tb = Toolbox(connect(tmp_path / "empty.db"), {})
    with pytest.raises(RuntimeError, match="Run main.py"):
        tb.get_movers("day", "gainers")


def test_unknown_tool_name(toolbox):
    with pytest.raises(ValueError, match="Unknown tool"):
        toolbox.call("delete_everything", {})


# ---------- agent loop, driven by a scripted fake model ----------

class FakeClient:
    """Stands in for the real model: returns pre-written replies and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.systems = []

    def converse(self, system, messages, tool_specs):
        self.calls.append(copy.deepcopy(messages))
        self.systems.append(system)
        return self.replies.pop(0)


def tool_reply(name, args, tool_id="t1"):
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": tool_id, "name": name, "input": args}}],
            }
        },
    }


def text_reply(text):
    return {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
    }


def test_agent_runs_tool_then_answers(toolbox):
    client = FakeClient(
        [
            tool_reply("get_movers", {"period": "week", "direction": "losers", "n": 2}),
            text_reply("DDD fell the most."),
        ]
    )
    answer = run_agent(client, "Worst stocks this week?", toolbox)
    assert answer == "DDD fell the most."
    last = client.calls[1][-1]  # what the model saw on its second turn
    assert last["role"] == "user"
    result = last["content"][0]["toolResult"]
    assert result["toolUseId"] == "t1" and result["status"] == "success"
    assert [s["ticker"] for s in result["content"][0]["json"]["stocks"]] == ["DDD", "BBB"]


def test_agent_passes_tool_errors_back_to_model(toolbox):
    client = FakeClient(
        [
            tool_reply("get_movers", {"period": "year", "direction": "gainers"}),
            text_reply("Sorry, I can only do day, week or month."),
        ]
    )
    answer = run_agent(client, "Best stocks this year?", toolbox)
    result = client.calls[1][-1]["content"][0]["toolResult"]
    assert result["status"] == "error"
    assert "period" in result["content"][0]["json"]["error"]
    assert answer.startswith("Sorry")


def test_agent_handles_several_tool_calls_in_one_turn(toolbox):
    two_calls = {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "a", "name": "get_outliers", "input": {}}},
                    {"toolUse": {"toolUseId": "b", "name": "get_top_companies", "input": {"n": 2}}},
                ],
            }
        },
    }
    client = FakeClient([two_calls, text_reply("done")])
    assert run_agent(client, "overview", toolbox) == "done"
    results = client.calls[1][-1]["content"]
    assert [r["toolResult"]["toolUseId"] for r in results] == ["a", "b"]


def test_agent_stops_after_max_steps(toolbox):
    loop = tool_reply("get_outliers", {})
    client = FakeClient([loop, loop, loop])
    answer = run_agent(client, "keep going", toolbox, max_steps=2)
    assert answer.startswith("I couldn't finish")
    assert len(client.calls) == 2



def test_get_stock(toolbox):
    res = toolbox.get_stock(" aaa ")
    assert res["stock"]["ticker"] == "AAA"
    assert res["stock"]["close"] == 100.0
    with pytest.raises(ValueError):
        toolbox.get_stock("ZZZ")



# ---------- groups ----------

GROUPS = {"alpha_beta": ["AAA", "BBB"], "solo": ["CCC", "ZZZ"]}


@pytest.fixture
def group_toolbox(conn):
    return Toolbox(conn, NAMES, theme_groups=GROUPS)


def test_load_groups_normalizes_names_and_tickers(tmp_path):
    f = tmp_path / "groups.csv"
    f.write_text("group,tickers\nAI Chips,aaa bbb\nBig Tech,AAA\n")
    assert load_groups(f) == {"ai chips": ["AAA", "BBB"], "big tech": ["AAA"]}


def test_load_groups_missing_file_means_no_groups(tmp_path):
    assert load_groups(tmp_path / "nope.csv") == {}


def test_group_overview_compares_all_groups(group_toolbox):
    res = group_toolbox.get_group_summary()
    assert set(res["groups"]) == {"alpha_beta", "solo"}
    ab = res["groups"]["alpha_beta"]
    assert ab["stocks_with_data"] == 2
    assert ab["month"]["average_pct"] == 1.0  # (10 + -8) / 2
    assert ab["day"]["up"] == 1 and ab["day"]["down"] == 1
    assert res["groups"]["solo"]["tickers_listed"] == 2   # ZZZ is listed...
    assert res["groups"]["solo"]["stocks_with_data"] == 1  # ...but has no data


def test_group_detail_lists_members_best_month_first(group_toolbox):
    res = group_toolbox.get_group_summary("ALPHA_BETA")  # capital letters do not matter
    assert res["group"] == "alpha_beta"
    assert [m["ticker"] for m in res["members_sorted_by_month"]] == ["AAA", "BBB"]
    assert res["week"]["median_pct"] == 0.5  # median of 4 and -3


def test_unknown_group_lists_available_names(group_toolbox):
    with pytest.raises(ValueError, match="alpha_beta"):
        group_toolbox.get_group_summary("crypto")


def test_groups_not_defined_gives_clear_error(toolbox):
    with pytest.raises(RuntimeError, match="groups.csv"):
        toolbox.get_group_summary()


# ---------- local model client (Ollama) ----------

def test_messages_are_translated_for_ollama():
    messages = [
        {"role": "user", "content": [{"text": "Worst this week?"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "get_movers", "input": {"period": "week"}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"json": {"as_of": "2026-11-02"}}], "status": "success"}}]},
    ]
    out = _to_ollama_messages("be careful", messages)
    assert out[0] == {"role": "system", "content": "be careful"}
    assert out[1] == {"role": "user", "content": "Worst this week?"}
    assert out[2]["tool_calls"][0]["function"] == {"name": "get_movers", "arguments": {"period": "week"}}
    assert out[3]["role"] == "tool" and out[3]["tool_name"] == "get_movers"
    assert '"as_of": "2026-11-02"' in out[3]["content"]


def test_tool_specs_are_translated_for_ollama():
    tools = _to_ollama_tools(TOOL_SPECS)
    assert len(tools) == len(TOOL_SPECS)
    first = tools[0]["function"]
    assert tools[0]["type"] == "function" and first["name"] == "get_movers"
    assert first["parameters"]["required"] == ["period", "direction"]


def test_ollama_reply_with_tool_call_becomes_tool_use():
    data = {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_outliers", "arguments": {}}}]}}
    reply = _from_ollama_response(data)
    assert reply["stopReason"] == "tool_use"
    use = reply["output"]["message"]["content"][0]["toolUse"]
    assert use["name"] == "get_outliers" and use["input"] == {}


def test_ollama_plain_reply_becomes_text():
    reply = _from_ollama_response({"message": {"role": "assistant", "content": "All quiet."}})
    assert reply["stopReason"] == "end_turn"
    assert reply["output"]["message"]["content"] == [{"text": "All quiet."}]


class FakeSession:
    """Pretends to be the network: returns canned Ollama replies and records what was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def post(self, url, json, timeout):
        self.sent.append((url, json))
        reply = self.replies.pop(0)

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return reply

        return Response()


def test_agent_works_end_to_end_through_local_client(toolbox):
    session = FakeSession(
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_movers", "arguments": {"period": "week", "direction": "losers", "n": 1}}}]}},
            {"message": {"role": "assistant", "content": "DDD fell the most."}},
        ]
    )
    client = LocalClient("qwen3:4b", session=session)
    assert run_agent(client, "Worst this week?", toolbox) == "DDD fell the most."
    url, body = session.sent[1]
    assert url.endswith("/api/chat") and body["model"] == "qwen3:4b" and body["think"] is False
    assert body["messages"][-1]["role"] == "tool"


def test_thinking_text_is_removed_from_local_replies():
    raw = "Okay, let me work this out...\n</think>\n\nHUM fell 12.98%."
    reply = _from_ollama_response({"message": {"role": "assistant", "content": raw}})
    assert reply["output"]["message"]["content"] == [{"text": "HUM fell 12.98%."}]
    assert _strip_thinking("<think>hmm</think>Done.") == "Done."

# ---------- run_agent_with_trace (used by /api/ask) ----------

def test_trace_lists_every_tool_used_in_order(toolbox):
    two_calls = {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "a", "name": "get_outliers", "input": {}}},
                    {"toolUse": {"toolUseId": "b", "name": "get_stock", "input": {"ticker": "AAA"}}},
                ],
            }
        },
    }
    client = FakeClient([two_calls, tool_reply("get_movers", {"period": "year", "direction": "gainers"}), text_reply("done")])
    result = run_agent_with_trace(client, "overview", toolbox)
    assert result["answer"] == "done"
    assert result["tools_used"] == [
        {"name": "get_outliers", "input": {}},
        {"name": "get_stock", "input": {"ticker": "AAA"}},
        {"name": "get_movers", "input": {"period": "year", "direction": "gainers"}},  # failed tools count too
    ]


def test_trace_without_tools_is_an_empty_list(toolbox):
    result = run_agent_with_trace(FakeClient([text_reply("Hi.")]), "hello", toolbox)
    assert result == {"answer": "Hi.", "tools_used": []}


def test_run_agent_still_returns_plain_text(toolbox):
    assert run_agent(FakeClient([text_reply("Hi.")]), "hello", toolbox) == "Hi."


def test_context_goes_into_system_prompt_not_the_question(toolbox):
    client = FakeClient([text_reply("ok")])
    run_agent_with_trace(client, "How is it doing?", toolbox, context="the user has AAA open.")
    assert client.systems[0].startswith(SYSTEM_PROMPT)
    assert client.systems[0].endswith("App context (set by the app, not the user): the user has AAA open.")
    assert client.calls[0] == [{"role": "user", "content": [{"text": "How is it doing?"}]}]


def test_no_context_means_plain_system_prompt(toolbox):
    client = FakeClient([text_reply("ok")])
    run_agent_with_trace(client, "q", toolbox)
    assert client.systems[0] == SYSTEM_PROMPT


def test_system_prompt_forbids_advice_and_asks_average_or_median():
    assert "Never tell the user to buy or sell, and never predict prices." in SYSTEM_PROMPT
    assert "average or the median" in SYSTEM_PROMPT


def test_past_deadline_stops_before_calling_the_model(toolbox):
    client = FakeClient([text_reply("should not be used")])
    result = run_agent_with_trace(client, "q", toolbox, deadline=time.monotonic() - 1)
    assert result == {"answer": TIME_LIMIT_ANSWER, "tools_used": []}
    assert client.calls == []


def test_deadline_passing_mid_question_keeps_tools_used(toolbox, monkeypatch):
    clock = iter([0.0, 100.0])  # first check: in time; second check (after one tool): too late
    monkeypatch.setattr("agent.time.monotonic", lambda: next(clock))
    client = FakeClient([tool_reply("get_outliers", {}), text_reply("unused")])
    result = run_agent_with_trace(client, "q", toolbox, deadline=50.0)
    assert result["answer"] == TIME_LIMIT_ANSWER
    assert result["tools_used"] == [{"name": "get_outliers", "input": {}}]
    assert len(client.calls) == 1


def test_local_client_passes_its_timeout_to_the_request():
    class RecordingSession(FakeSession):
        def post(self, url, json, timeout):
            self.timeout = timeout
            return super().post(url, json, timeout)

    session = RecordingSession([{"message": {"role": "assistant", "content": "ok"}}])
    LocalClient("qwen3:4b", session=session, timeout=120).converse("s", [{"role": "user", "content": [{"text": "q"}]}], [])
    assert session.timeout == 120
    assert LocalClient("qwen3:4b", session=session).timeout == 300  # default unchanged for the CLI
