"""
GitHub webhook handler that triggers the self-hosted OpenHands agent.

Supported triggers
------------------
* An issue is labeled **openhands**  →  agent works on the issue
* A comment on an issue or PR contains **@openhands**  →  agent works on the task

Flow
----
1. FastAPI validates the signature and immediately returns 202 Accepted.
2. The agent runs in a BackgroundTask — GitHub never times out waiting.
3. When the agent finishes it posts the result back as a GitHub comment.

Setup
-----
1. Copy .env.example → .env and fill in the values.
2. Run `docker compose up`.
3. In your GitHub repo → Settings → Webhooks:
   - Payload URL : http://<your-server>:8080/webhook/github
   - Content type: application/json
   - Secret      : value of GITHUB_WEBHOOK_SECRET from your .env
   - Events      : Issues, Issue comments
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from agent.simple_agent import SimpleAgent
from webhook.github_client import GitHubClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="OpenHands GitHub Webhook")

# ── Config from environment ───────────────────────────────────────────────────
WEBHOOK_SECRET: str = os.environ["GITHUB_WEBHOOK_SECRET"]
GITHUB_TOKEN: str = os.environ["GITHUB_TOKEN"]
OPENHANDS_BASE_URL: str = os.environ.get("OPENHANDS_BASE_URL", "http://openhands:3000")

github = GitHubClient(GITHUB_TOKEN)
agent = SimpleAgent(OPENHANDS_BASE_URL)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_signature(payload: bytes, signature: str | None) -> None:
    if not signature:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256")
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


def _build_task(title: str, body: str, repo_url: str) -> str:
    return (
        f"Repository: {repo_url}\n\n"
        f"## Task\n{title}\n\n"
        f"## Details\n{body or 'No details provided.'}\n\n"
        "Please analyse the request, make necessary code changes, and summarise what you did."
    )


def _format_result(result) -> str:
    status_emoji = {"stopped": "✅", "error": "❌", "timeout": "⏱️"}.get(result.status, "ℹ️")
    msg = result.last_message or "_No output returned._"
    return (
        f"{status_emoji} **OpenHands finished** (status: `{result.status}`)\n\n"
        f"{msg}\n\n"
        f"<sub>Conversation ID: `{result.conversation_id}`</sub>"
    )


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.post("/webhook/github", status_code=202)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
) -> JSONResponse:
    payload_bytes = await request.body()
    _verify_signature(payload_bytes, x_hub_signature_256)
    event = await request.json()

    if x_github_event == "issues":
        task_fn = _handle_issue_event(event)
        if task_fn is not None:
            background_tasks.add_task(_run, task_fn)

    elif x_github_event == "issue_comment":
        task_fn = _handle_comment_event(event)
        if task_fn is not None:
            background_tasks.add_task(_run, task_fn)

    # Return immediately — agent work happens in the background
    return JSONResponse({"status": "accepted"}, status_code=202)


async def _run(coro) -> None:
    """Await a coroutine inside a BackgroundTask, logging any exceptions."""
    try:
        await coro
    except Exception:
        logger.exception("Background agent task failed")


# ── Event handlers — return a coroutine or None ───────────────────────────────

def _handle_issue_event(event: dict):
    if event.get("action") != "labeled":
        return None
    if event.get("label", {}).get("name") != "openhands":
        return None

    issue = event["issue"]
    repo = event["repository"]
    owner, repo_name = repo["full_name"].split("/")
    task = _build_task(issue["title"], issue["body"], repo["html_url"])
    logger.info("Issue #%d labeled 'openhands' — queuing agent", issue["number"])

    return _run_agent_and_reply(owner, repo_name, issue["number"], task)


def _handle_comment_event(event: dict):
    if event.get("action") != "created":
        return None
    comment_body: str = event.get("comment", {}).get("body", "")
    if "@openhands" not in comment_body:
        return None

    issue = event["issue"]
    repo = event["repository"]
    owner, repo_name = repo["full_name"].split("/")
    instruction = comment_body.replace("@openhands", "").strip()
    task = _build_task(
        f"Requested via comment on #{issue['number']}: {issue['title']}",
        instruction or issue.get("body", ""),
        repo["html_url"],
    )
    logger.info("Comment trigger on #%d — queuing agent", issue["number"])

    return _run_agent_and_reply(owner, repo_name, issue["number"], task)


async def _run_agent_and_reply(owner: str, repo_name: str, issue_number: int, task: str) -> None:
    await github.post_issue_comment(
        owner, repo_name, issue_number,
        "**OpenHands** is on it. I'll post an update here when done.",
    )
    result = await agent.run(task)
    await github.post_issue_comment(owner, repo_name, issue_number, _format_result(result))


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})
