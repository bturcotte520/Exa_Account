import asyncio
import re

import httpx

from .models import new_id, now


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.sub(r"[^a-z]", "", key.lower()) in {
            "apikey", "exakey", "jevkey", "findata", "authorization", "xapikey", "webhooksecret", "secret", "signature", "token", "localtoken", "localmonitortoken"
        } else sanitize(item, secrets)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(/webhooks/exa/)[^\s?\"/]+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)([?&](?:api_key|key|token|secret)=)[^&\s]+", r"\1[REDACTED]", value)
    return value


class ExaError(RuntimeError):
    def __init__(self, message, *, ambiguous=False, status_code=None):
        super().__init__(message)
        self.ambiguous = ambiguous
        self.status_code = status_code


class ExaClient:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store

    async def request(self, method, path, body=None, account_id=None, operation=None):
        if not self.settings.api_key:
            raise ExaError("Configure the server-side exa_key before making requests")
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expected an Exa API path")
        method = method.upper()
        for attempt in range(1, 4 if method == "GET" else 2):
            artifact = {"id": new_id("artifact"), "account_id": account_id,
                        "operation": operation or f"{method} {path}", "started_at": now(),
                        "attempt": attempt, "request": {"method": method, "path": path, "body": body},
                        "response": None, "usage": None, "cost_dollars": None}
            self.store.put("artifacts", sanitize(artifact, (self.settings.api_key,)))
            retry = False
            try:
                async with httpx.AsyncClient(timeout=45, follow_redirects=False) as http:
                    response = await http.request(method, "https://api.exa.ai" + path,
                                                  json=body, headers={"x-api-key": self.settings.api_key})
                try:
                    data = response.json()
                except ValueError:
                    data = {"error": "Non-JSON response"}
                artifact.update(response=data, status_code=response.status_code,
                                request_id=response.headers.get("x-request-id") or (data.get("requestId") if isinstance(data, dict) else None))
                if response.is_error:
                    retry = method == "GET" and (response.status_code == 429 or response.status_code >= 500)
                    raise ExaError(f"Exa request failed (HTTP {response.status_code}); inspect artifact", ambiguous=method != "GET" and response.status_code >= 500, status_code=response.status_code)
                if not isinstance(data, dict):
                    raise ExaError("Exa response was not an object", ambiguous=method != "GET")
                artifact.update(usage=data.get("usage"), cost_dollars=data.get("costDollars"))
                return data
            except httpx.RequestError as exc:
                retry = method == "GET"
                error = ExaError("Exa connection failed; inspect persisted operation before retrying", ambiguous=method != "GET")
                artifact["error"] = str(error)
                if not retry or attempt == 3:
                    raise error from exc
            except ExaError as exc:
                artifact["error"] = str(exc)
                if not retry or attempt == 3:
                    raise
            finally:
                artifact["ended_at"] = now()
                self.store.put("artifacts", sanitize(artifact, (self.settings.api_key,)))
            await asyncio.sleep(0.25 * 2 ** (attempt - 1))
