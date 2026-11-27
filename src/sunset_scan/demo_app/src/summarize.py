"""Weekly client summaries."""
import os

from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
SUMMARY_MODEL = "gpt-5-mini"


def summarize(text: str) -> str:
    resp = client.responses.create(model=SUMMARY_MODEL, input=f"Summarize for a client:\n{text}")
    return resp.output_text


def legacy_thread_summary(thread_id: str) -> str:
    # Old path, still wired into the Friday report.
    run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=os.environ["ASSISTANT_ID"])
    return run.id
