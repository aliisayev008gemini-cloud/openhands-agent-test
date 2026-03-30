"""
GitHub webhook handler that triggers the self-hosted OpenHands agent.

Supported triggers
------------------
* An issue is labeled **openhands**  →  agent works on the issue
* A comment on an issue or PR contains **@openhands**  →  agent works on the task

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

import hashlib
import hmac
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request
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


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
) -> JSONResponse:
    payload_bytes = await request.body()
    _verify_signature(payload_bytes, x_hub_signature_256)
    event = await request.json()

    if x_github_event == "issues":
        await _handle_issue_event(event)

    elif x_github_event == "issue_comment":
        await _handle_comment_event(event)

    return JSONResponse({"status": "ok"})


# ── Event handlers ────────────────────────────────────────────────────────────

async def _handle_issue_event(event: dict) -> None:
    action: str = event.get("action", "")
    if action != "labeled":
        return
    if event.get("label", {}).get("name") != "openhands":
        return

    issue = event["issue"]
    repo = event["repository"]
    owner, repo_name = repo["full_name"].split("/")

    task = _build_task(issue["title"], issue["body"], repo["html_url"])
    logger.info("Issue #%d labeled 'openhands' — starting agent", issue["number"])

    await github.post_issue_comment(
        owner, repo_name, issue["number"],
        "**OpenHands** is working on this issue. I'll post an update when done.",
    )
    result = await agent.run(task)
    reply = _format_result(result)
    await github.post_issue_comment(owner, repo_name, issue["number"], reply)


async def _handle_comment_event(event: dict) -> None:
    action: str = event.get("action", "")
    if action != "created":
        return
    comment_body: str = event.get("comment", {}).get("body", "")
    if "@openhands" not in comment_body:
        return

    issue = event["issue"]
    repo = event["repository"]
    owner, repo_name = repo["full_name"].split("/")

    # Strip the mention so the agent sees the actual instruction
    instruction = comment_body.replace("@openhands", "").strip()
    task = _build_task(
        f"Requested via comment on #{issue['number']}: {issue['title']}",
        instruction or issue.get("body", ""),
        repo["html_url"],
    )
    logger.info("Comment trigger on #%d — starting agent", issue["number"])

    await github.post_issue_comment(
        owner, repo_name, issue["number"],
        "**OpenHands** received your request and is working on it.",
    )
    result = await agent.run(task)
    reply = _format_result(result)
    await github.post_issue_comment(owner, repo_name, issue["number"], reply)


def _format_result(result) -> str:
    status_emoji = {"stopped": "✅", "error": "❌", "timeout": "⏱️"}.get(result.status, "ℹ️")
    msg = result.last_message or "_No output returned._"
    return (
        f"{status_emoji} **OpenHands finished** (status: `{result.status}`)\n\n"
        f"{msg}\n\n"
        f"<sub>Conversation ID: `{result.conversation_id}`</sub>"
    )


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})
