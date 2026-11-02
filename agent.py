"""A small tool-calling agent for the stock data.

    python agent.py "Which stocks fell the most this month?"

Needs AWS credentials and the BEDROCK_MODEL_ID environment variable (the model or
inference-profile ID shown in the Amazon Bedrock console).
"""
import os
import sys

import config
from storage import connect
from tools import TOOL_SPECS, Toolbox
from universe import load_universe

MAX_STEPS = 8  # stop runaway loops: at most this many model calls per question

SYSTEM_PROMPT = """You are a careful market data assistant for a personal stock tracker.

Rules:
- Every number you state must come from a tool result. Never use numbers from memory.
- If the tools cannot answer the question, say so plainly instead of guessing.
- Say which trading day the data is from (the as_of field).
- Weekly and monthly returns are rolling windows of 5 and 21 trading days.
- Very large one-day moves can be bad data, so mention that when one appears.
- Keep answers short and plain. This is information, not investment advice."""


def run_agent(client, question, toolbox, max_steps=MAX_STEPS, verbose=False):
    """Ask the model, run any tools it requests, give it the results, repeat until it answers."""
    messages = [{"role": "user", "content": [{"text": question}]}]
    for _ in range(max_steps):
        response = client.converse(SYSTEM_PROMPT, messages, TOOL_SPECS)
        message = response["output"]["message"]
        messages.append(message)

        if response["stopReason"] != "tool_use":  # the model is done and has written its answer
            return "".join(block["text"] for block in message["content"] if "text" in block)

        results = []
        for block in message["content"]:
            if "toolUse" not in block:
                continue
            call = block["toolUse"]
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

    return "I couldn't finish within the step limit. Try a narrower question."


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


def main():
    if len(sys.argv) < 2:
        sys.exit('Usage: python agent.py "your question"')
    model_id = os.environ.get("BEDROCK_MODEL_ID")
    if not model_id:
        sys.exit("Set BEDROCK_MODEL_ID first (copy the model or inference-profile ID from the Bedrock console).")

    universe = load_universe(config.TICKERS_CSV)
    names = dict(zip(universe["ticker"], universe["name"]))
    toolbox = Toolbox(connect(config.DB_PATH), names, config.SAME_COMPANY_GROUPS, config.OUTLIER_DAY_PCT)
    print(run_agent(BedrockClient(model_id), " ".join(sys.argv[1:]), toolbox, verbose=True))


if __name__ == "__main__":
    main()