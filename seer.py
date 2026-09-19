"""B4b Seer client — a USABLE issue-analysis loop over the Sentry API (stdlib).

Covers the Seer surface that a sandbox harness needs, verified against the live
Sentry API and pinned to the endpoint shapes in getsentry/sentry
``src/sentry/api/urls.py`` (the live source is the only authority):

- ``GET  /organizations/{org}/seer/setup-check/``      -> ``setup_check()``
- ``GET  /projects/{org}/{project}/seer/preferences/`` -> ``project_preferences()``
- ``POST /organizations/{org}/issues/{issue_id}/summarize/``  -> ``summarize_issue()``
  (Seer AI issue analysis: headline / whatsWrong / possibleCause / scores)
- ``POST /organizations/{org}/issues/{issue_id}/autofix/``    -> ``trigger_autofix()``
- ``GET  /organizations/{org}/issues/{issue_id}/autofix/``    -> ``autofix_status()``

Autofix runs require repositories connected to the project; the live sandbox
returned 409 ``repos_not_linked`` at provisioning time (documented in
``docs/telemetry/sandbox-flow.md``), so ``trigger_autofix`` reports that
verbatim instead of raising — the summary loop is the verified usable path.

Credentials (telemetry-contract.md §3.4): the auth token enters via the env
var NAME ``SENTRY_AUTH_TOKEN`` only. Token values never appear in code, docs,
logs or fixtures. Like ``sinks.py``, failures never raise out of the public
methods — they come back as ``{"ok": bool, "status": int | None, "body": ...}``.
"""

from __future__ import annotations

import json
import os
from typing import Callable
from urllib import error as _urlerror
from urllib import request as _urlrequest

__all__ = ["SeerClient"]

DEFAULT_BASE_URL = "https://sentry.io/api/0"


def _default_request(
    url: str, method: str, headers: dict, body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    """One blocking HTTP request. Returns ``(status, body_bytes)``.

    Raises ``urllib.error.URLError``/``HTTPError``/``socket.timeout`` on
    transport failure — callers normalize to the result dict.
    """
    req = _urlrequest.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with _urlrequest.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except _urlerror.HTTPError as exc:
        # HTTP error statuses still carry a JSON body worth returning.
        return int(exc.code), exc.read()


class SeerClient:
    """Seer issue-analysis loop against one organization's Sentry API."""

    DEFAULT_ENV_TOKEN = "SENTRY_AUTH_TOKEN"
    DEFAULT_ORG = "sovav"
    DEFAULT_PROJECT = "sovav-hermes-sandbox"

    def __init__(
        self,
        api_token: str | None = None,
        org: str | None = None,
        project: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
        request: Callable[[str, str, dict, bytes | None, float], tuple[int, bytes]] | None = None,
    ):
        self.api_token = api_token or os.environ.get(self.DEFAULT_ENV_TOKEN) or ""
        self.org = org or os.environ.get("SENTRY_ORG") or self.DEFAULT_ORG
        self.project = project or os.environ.get("SENTRY_PROJECT") or self.DEFAULT_PROJECT
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._request = request or _default_request

    @property
    def configured(self) -> bool:
        return bool(self.api_token)

    def _headers(self) -> dict:
        # The token value never appears in payloads, logs or detail strings.
        return {"Authorization": f"Bearer {self.api_token}"}

    def _call(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        """Run one API call; normalize every failure into the result dict."""
        if not self.configured:
            return {
                "ok": False,
                "status": None,
                "body": None,
                "detail": f"no credentials: set {self.DEFAULT_ENV_TOKEN} (env, never inline)",
            }
        url = f"{self.base_url}{path}"
        body = None
        headers = self._headers()
        if payload is not None or method == "POST":
            body = json.dumps(payload or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._request(url, method, headers, body, self.timeout)
        except Exception as exc:  # noqa: BLE001 — normalized to the result dict
            return {
                "ok": False,
                "status": None,
                "body": None,
                "detail": f"{type(exc).__name__}",
            }
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            parsed = {"_raw": raw[:200].decode("utf-8", errors="replace")}
        return {"ok": 200 <= status < 300, "status": status, "body": parsed}

    # ------------------------------------------------------------------
    # Seer surface (paths pinned to getsentry/sentry src/sentry/api/urls.py)
    # ------------------------------------------------------------------

    def setup_check(self) -> dict:
        """``GET /organizations/{org}/seer/setup-check/`` — Seer availability."""
        return self._call(f"/organizations/{self.org}/seer/setup-check/")

    def project_preferences(self, project: str | None = None) -> dict:
        """``GET /projects/{org}/{project}/seer/preferences/``."""
        p = project or self.project
        return self._call(f"/projects/{self.org}/{p}/seer/preferences/")

    def summarize_issue(self, issue_id: str | int, event_id: str | None = None) -> dict:
        """``POST /organizations/{org}/issues/{issue_id}/summarize/``.

        The verified usable Seer analysis loop: Sentry sends the issue's
        metadata to Seer and returns ``headline``/``whatsWrong``/
        ``possibleCause``/``scores``.

        ``event_id`` (a Sentry event id, hex) maps to the endpoint's
        ``force_event_id`` (getsentry/sentry ``src/sentry/seer/endpoints/
        group_ai_summary.py`` — verified against live source): the summary is
        generated for THAT specific event, and the response's ``eventId``
        names it. Without it Seer picks the event itself, which can be an
        older sibling of the group rather than the event being proven.
        """
        payload = {"event_id": event_id} if event_id else None
        return self._call(
            f"/organizations/{self.org}/issues/{issue_id}/summarize/",
            method="POST",
            payload=payload,
        )

    def trigger_autofix(self, issue_id: str | int) -> dict:
        """``POST /organizations/{org}/issues/{issue_id}/autofix/``.

        Requires repositories connected to the project; without them Sentry
        answers 409 ``repos_not_linked`` — surfaced verbatim, never raised.
        """
        return self._call(f"/organizations/{self.org}/issues/{issue_id}/autofix/", method="POST")

    def autofix_status(self, issue_id: str | int) -> dict:
        """``GET /organizations/{org}/issues/{issue_id}/autofix/``."""
        return self._call(f"/organizations/{self.org}/issues/{issue_id}/autofix/")
