"""GitHub API helpers used by the webhook handler."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def post_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> None:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=self._headers, json={"body": body})
            resp.raise_for_status()
            logger.info("Posted comment to %s/%s#%d", owner, repo, issue_number)

    async def post_pr_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        # PR comments go to the issues endpoint (GitHub treats them the same)
        await self.post_issue_comment(owner, repo, pr_number, body)

    async def add_label(self, owner: str, repo: str, issue_number: int, label: str) -> None:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/labels"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=self._headers, json={"labels": [label]})
            if resp.status_code not in (200, 201):
                logger.warning("Could not add label: %s", resp.text)