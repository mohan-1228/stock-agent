"""A small tool-calling agent for the stock data.

    python agent.py "Which stocks fell the most this month?"

Needs AWS credentials and the BEDROCK_MODEL_ID environment variable (the model or
inference-profile ID shown in the Amazon Bedrock console).
"""
import json
import os
import sys
import time

import config
from storage import connect
from tools import TOOL_SPECS, Toolbox
from universe import load_groups, load_universe

MAX_STEPS = 8  # stop runaway loops: at most this many model calls per question

SYSTEM_PROMPT = """You are a careful market data assistant for a personal stock tracker.

Rules:
- Every number you state must come from a tool result. Never use numbers from memory.
- If the tools cannot answer the question, say so plainly instead of guessing.
- Say which trading day the data is from (the as_of field).
- Weekly and monthly returns are rolling windows of 5 and 21 trading days.
- Very large one-day moves can be bad data, so mention that when one appears.
- When you give a group's return, say whether it is the average or the median.
- Never tell the user to buy or sell, and never predict prices.
- Keep answers short and plain. This is information, not investment advice."""

STEP_LIMIT_ANSWER = "I couldn't finish within the step limit. Try a narrower question."
TIME_LIMIT_ANSWER = "I ran out of time before finishing. Try a shorter question."


def run_agent(client, question, toolbox, max_steps=MAX_STEPS, verbose=False):
    """Ask the model, run any tools it requests, give it the results, repeat until it answers."""
    return run_agent_with_trace(client, question, toolbox, max_steps=max_steps, verbose=verbose)["answer"]


def run_agent_with_trace(client, question, toolbox, context=None, deadline=None, max_steps=MAX_STEPS, verbose=False):
    """Same loop as run_agent, but also reports which tools were used (for transparency on the page).

    context:  optional sentence written by the app (never by the user), e.g. which stock is open.
              It goes into the system prompt, separate from the user's question.
    deadline: optional time.monotonic() value. The loop checks it before each model call and stops
              when it has passed, so a timed-out question does not keep the model busy for long.
    Returns {"answer": str, "tools_used": [{"name": ..., "input": ...}, ...]}.
    """
    system = SYSTEM_PROMPT
    if context:
        system += f"\n\nApp context (set by the app, not the user): {context}"
    messages = [{"role": "user", "content": [{"text": question}]}]
    tools_used = []

    for _ in range(max_steps):
        if deadline is not None and time.monotonic() > deadline:
            return {"answer": TIME_LIMIT_ANSWER, "tools_used": tools_used}
        response = client.converse(system, messages, TOOL_SPECS)
        message = response["output"]["message"]
        messages.append(message)

        if response["stopReason"] != "tool_use":  # the model is done and has written its answer
            answer = "".join(block["text"] for block in message["content"] if "text" in block)
            return {"answer": answer, "tools_used": tools_used}

        results = []
        for block in message["content"]:
            if "toolUse" not in block:
                continue
            call = block["toolUse"]
            tools_used.append({"name": call["name"], "input": call["input"]})
            if verbose:
                print(f"  -> tool: {call['name']}({call['input']})")
            try:
                payload, status = toolbox.call(call["name"], call["input"]), "success"
            except Exception as exc:  # tell the model what went wrong so it can adjust
                payload, status = {"error": str(exc)}, "error"
            results.append(
                {
                    "toolResult": {
                        "toolUseId": call["toolUseId"],
                        "content": [{"json": payload}],
                        "status": status,
                    }
                }
            )
        messages.append({"role": "user", "content": results})

    return {"answer": STEP_LIMIT_ANSWER, "tools_used": tools_used}


class BedrockClient:
    """The only piece that talks to AWS. Everything else runs fine without it."""

    def __init__(self, model_id):
        import boto3  # imported here so the rest of the project works without boto3

        self.model_id = model_id
        self.runtime = boto3.client("bedrock-runtime")

    def converse(self, system, messages, tool_specs):
        return self.runtime.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=messages,
            toolConfig={"tools": tool_specs},
            inferenceConfig={"maxTokens": 1500},
        )


def _to_ollama_messages(system, messages):
    """Translate our Bedrock-style messages into the format Ollama's /api/chat expects."""
    out = [{"role": "system", "content": system}]
    tool_names = {}  # toolUseId -> tool name, so results can say which tool they came from
    for message in messages:
        blocks = message["content"]
        if message["role"] == "assistant":
            text = "".join(b["text"] for b in blocks if "text" in b)
            calls = []
            for b in blocks:
                if "toolUse" in b:
                    use = b["toolUse"]
                    tool_names[use["toolUseId"]] = use["name"]
                    calls.append({"function": {"name": use["name"], "arguments": use["input"]}})
            item = {"role": "assistant", "content": text}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
        else:
            for b in blocks:
                if "text" in b:
                    out.append({"role": "user", "content": b["text"]})
                elif "toolResult" in b:
                    result = b["toolResult"]
                    payload = result["content"][0]["json"]
                    out.append(
                        {
                            "role": "tool",
                            "tool_name": tool_names.get(result["toolUseId"], ""),
                            "content": json.dumps(payload),
                        }
                    )
    return out


def _to_ollama_tools(tool_specs):
    """Our TOOL_SPECS are in Bedrock format; Ollama wants the OpenAI-style 'function' format."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec["toolSpec"]["name"],
                "description": spec["toolSpec"]["description"],
                "parameters": spec["toolSpec"]["inputSchema"]["json"],
            },
        }
        for spec in tool_specs
    ]

def _strip_thinking(text):
    """Remove the model's private 'thinking' text, keeping only the final answer."""
    if "</think>" in text:
        text = text.split("</think>")[-1]  # the real answer comes after the last tag
    return text.replace("<think>", "").strip()


def _from_ollama_response(data):
    """Translate Ollama's reply back into the Bedrock-style shape run_agent understands."""
    message = data["message"]
    content = []
    text = _strip_thinking(message.get("content") or "")
    if text:
        content.append({"text": text})
    calls = message.get("tool_calls") or []
    for number, call in enumerate(calls):
        function = call["function"]
        content.append(
            {
                "toolUse": {
                    "toolUseId": f"call_{number}_{function['name']}",
                    "name": function["name"],
                    "input": function.get("arguments") or {},
                }
            }
        )
    return {
        "stopReason": "tool_use" if calls else "end_turn",
        "output": {"message": {"role": "assistant", "content": content}},
    }


class LocalClient:
    """Talks to a model running on this computer through Ollama. Free and private."""

    def __init__(self, model, url="http://localhost:11434", num_ctx=8192, session=None, timeout=300):
        import requests  # imported here, same idea as boto3 above

        self.model = model
        self.timeout = timeout  # seconds to wait for one model reply
        self.url = url.rstrip("/")
        self.num_ctx = num_ctx
        self.session = session or requests

    def converse(self, system, messages, tool_specs):
        body = {
            "model": self.model,
            "messages": _to_ollama_messages(system, messages),
            "tools": _to_ollama_tools(tool_specs),
            "stream": False,
            "think": False,  # skip the long "thinking" text; we want fast tool calls
            "options": {"num_ctx": self.num_ctx, "temperature": 0},
        }
        response = self.session.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        response.raise_for_status()
        return _from_ollama_response(response.json())


def make_client():
    """Pick the model backend from environment variables."""
    local_model = os.environ.get("OLLAMA_MODEL")
    if local_model:
        return LocalClient(local_model)
    model_id = os.environ.get("BEDROCK_MODEL_ID")
    if model_id:
        return BedrockClient(model_id)
    sys.exit("Set OLLAMA_MODEL (for example qwen3:4b) or BEDROCK_MODEL_ID first.")


def main():
    if len(sys.argv) < 2:
        sys.exit('Usage: python agent.py "your question"')
    client = make_client()

    universe = load_universe(config.TICKERS_CSV)
    names = dict(zip(universe["ticker"], universe["name"]))
    toolbox = Toolbox(
        connect(config.DB_PATH), names, config.SAME_COMPANY_GROUPS, config.OUTLIER_DAY_PCT,
        theme_groups=load_groups(config.GROUPS_CSV),
    )
    print(run_agent(client, " ".join(sys.argv[1:]), toolbox, verbose=True))

if __name__ == "__main__":
    main()