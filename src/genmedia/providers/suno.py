"""Third-party SunoAPI adapter, NOT an official Suno SDK.

The prompt payload follows the documented API schema. The old archive's
undocumented lyrics/duration payload is an explicit opt-in, never inferred from
a model's name. Paid POSTs are never automatically retried.
"""
from __future__ import annotations
import ipaddress
import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urljoin
import httpx
from ..models import Candidate
from ..errors import ProviderError, SubmissionRejected, SubmissionUncertain
from ..security import redact


def public_https(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ProviderError("An HTTPS URL without embedded credentials is required")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ProviderError("Local service URLs are not allowed")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ProviderError("Private IP URLs are not allowed")
    except ValueError:
        pass


from .suno_schema import normalize_suno, song_text


class SunoAPI:
    def __init__(self, cfg, client=None, sleep=time.sleep):
        self.cfg, self.sleep = cfg, sleep
        public_https(cfg.base_url)
        if urlsplit(cfg.base_url).query or urlsplit(cfg.base_url).fragment:
            raise ProviderError("API base URL must not contain query parameters or a fragment")
        self.base = cfg.base_url.rstrip("/")
        if client is None:
            key = os.environ.get("SUNO_API_KEY", "").strip()
            if not key:
                raise ProviderError("SUNO_API_KEY is missing")
            client = httpx.Client(headers={"Authorization": f"Bearer {key}"},
                                   timeout=httpx.Timeout(60, connect=15), follow_redirects=False)
        self.client = client

    def request(self, method, endpoint, **kwargs):
        method = method.upper()
        submitting = method == "POST" and endpoint == "/generate"
        attempts = 3 if method == "GET" else 1
        # Conservative allowlist of explicit parameter/auth/rate-limit refusals.
        # 408, 5xx, redirects and malformed responses do NOT prove no task exists.
        rejected_codes = {400, 401, 402, 403, 404, 405, 413, 415, 422, 429}

        def failure(message, *, definite_rejection=False):
            if submitting:
                cls = SubmissionRejected if definite_rejection else SubmissionUncertain
            elif method == "POST" and not definite_rejection:
                cls = SubmissionUncertain
            else:
                cls = ProviderError
            return cls(message)

        for attempt in range(attempts):
            try:
                response = self.client.request(method, self.base + endpoint, **kwargs)
            except httpx.TransportError:
                if attempt + 1 < attempts:
                    self.sleep(2 ** attempt)
                    continue
                raise failure("SunoAPI transport failure; POST outcome may be unknown") from None

            status = response.status_code
            if status >= 500 and method == "GET" and attempt + 1 < attempts:
                self.sleep(2 ** attempt)
                continue
            if status != 200:
                raise failure(
                    f"SunoAPI HTTP {status}; not accepted as success",
                    definite_rejection=status in rejected_codes,
                )
            try:
                body = response.json()
            except (ValueError, TypeError):
                raise failure("SunoAPI returned unreadable JSON; POST outcome may be unknown") from None
            if not isinstance(body, dict):
                raise failure("SunoAPI returned a non-object response")
            code = body.get("code")
            if type(code) is not int or code != 200:
                safe_code = code if type(code) is int else "invalid"
                msg = body.get("msg")
                detail = ": " + redact(msg, limit=300) if isinstance(msg, str) else ""
                # Contradictory task metadata is not a definite refusal.
                data = body.get("data")
                has_task = isinstance(data, dict) and bool(data.get("taskId"))
                raise failure(
                    f"SunoAPI business status {safe_code}{detail} (not auto-retried)",
                    definite_rejection=type(code) is int and code in rejected_codes and not has_task,
                )
            data = body.get("data")
            if not isinstance(data, dict):
                raise failure("SunoAPI response has no data object; POST outcome may be unknown")
            return data
        raise ProviderError("SunoAPI read attempts exhausted")

    def close(self):
        self.client.close()


class SunoAudio:
    def __init__(self, cfg, client=None, download_client=None, sleep=time.sleep):
        self.cfg = cfg
        self.api = SunoAPI(cfg, client, sleep)
        self.download_client = download_client or httpx.Client(timeout=60, follow_redirects=False)

    def preflight_submit(self):
        if not self.cfg.callback_url:
            raise SubmissionRejected(
                "SUNO_CALLBACK_URL is required by SunoAPI even when polling is used"
            )

        try:
            public_https(self.cfg.callback_url)
        except ProviderError as exc:
            raise SubmissionRejected(redact(exc)) from None

        if self.cfg.request_format == "prompt" and self.cfg.include_duration and self.cfg.model != "V5_5":
            raise SubmissionRejected(
                "Documented duration requires V5_5; disable include_duration for this model"
            )

    def submit(self, script, cfg):
        self.preflight_submit()
        text = song_text(script)
        limit = 3000 if self.cfg.model in {"V3_5", "V4"} else 5000
        style_limit = 200 if self.cfg.model in {"V3_5", "V4"} else 1000
        if len(text) > limit or len(cfg.style) > style_limit or not cfg.style.strip():
            raise SubmissionRejected("Suno text/style exceeds model limits or style is empty")
        payload = {
            "customMode": True,
            "instrumental": False,
            "title": script.title,
            "style": cfg.style,
            "model": self.cfg.model,
            "callBackUrl": self.cfg.callback_url,
        }

        if self.cfg.request_format == "legacy_lyrics":
            payload.update(lyrics=text, duration=cfg.duration_seconds)
        else:
            payload["prompt"] = text
            if self.cfg.include_duration:
                payload["duration"] = cfg.duration_seconds

        result = self.api.request("POST", "/generate", json=payload)

        task = result.get("taskId")

        if not isinstance(task, str) or not task.strip() or len(task) > 200:
            raise SubmissionUncertain(
                "SunoAPI reported success but returned no valid taskId; "
                "check dashboard before retrying"
            )

        return task

    def poll(self, task_id):
        data = self.api.request("GET", "/generate/record-info", params={"taskId": task_id})
        status = data.get("status")
        if status in {"PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS"}:
            return None
        if status != "SUCCESS":
            safe = status if status in {"CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "CALLBACK_EXCEPTION", "SENSITIVE_WORD_ERROR"} else "UNKNOWN"
            message = data.get("errorMessage")
            detail = ": " + redact(message, limit=300) if isinstance(message, str) else ""
            raise ProviderError(f"SunoAPI terminal or unknown status: {safe}{detail}")
        response = data.get("response")
        rows = response.get("sunoData") if isinstance(response, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("Invalid SunoAPI candidate list")
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderError("Invalid SunoAPI candidate")
            audio_id, url = row.get("id"), row.get("audioUrl") or row.get("audio_url")
            if not isinstance(audio_id, str) or not isinstance(url, str):
                raise ProviderError("SunoAPI SUCCESS contains an incomplete audio candidate")
            public_https(url)
            result.append(Candidate(id=audio_id, url=url))
        if not result or len(result) > 16 or len({x.id for x in result}) != len(result):
            raise ProviderError("Empty, duplicate or excessive SunoAPI candidates")
        return result

    def download(self, candidate, path: Path):
        url = candidate.url
        try:
            for _ in range(6):
                public_https(url)
                with self.download_client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ProviderError("Audio redirect without a location")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise ProviderError(f"Audio download HTTP {response.status_code}")
                    size = 0
                    with path.open("wb") as out:
                        for chunk in response.iter_bytes(65536):
                            size += len(chunk)
                            if size > 100*1024*1024:
                                raise ProviderError("Audio download exceeds 100 MiB limit")
                            out.write(chunk)
                    if not size:
                        raise ProviderError("Downloaded audio is empty")
                    return
            raise ProviderError("Too many audio redirects")
        except httpx.TransportError:
            raise ProviderError("Audio download transport failure") from None

    def close(self):
        self.api.close()
        self.download_client.close()


class SunoAlignment:
    billable = True

    def __init__(self, cfg, client=None, sleep=time.sleep):
        self.api = SunoAPI(cfg, client, sleep)

    def align(self, request):
        data = self.api.request("POST", "/generate/get-timestamped-lyrics",
                               json={"taskId": request.task_id, "audioId": request.candidate.id})
        return normalize_suno(data.get("alignedWords"))

    def close(self):
        self.api.close()
