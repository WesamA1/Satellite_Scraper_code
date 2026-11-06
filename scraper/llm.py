"""A local language model, for Discovery and code drafting without the Anthropic
API.

Talks to any server with the OpenAI-style chat API (POST /chat/completions):
Ollama (http://localhost:11434/v1), vLLM (http://localhost:8000/v1),
llama.cpp's server, LM Studio. Plain HTTP via requests, so no extra package.

Settings come from .env:
    LOCAL_LLM_URL          e.g. http://localhost:11434/v1
    LOCAL_LLM_MODEL        the model for Discovery, e.g. qwen3:32b
    LOCAL_CODEGEN_MODEL    the model for drafting source code (defaults to LOCAL_LLM_MODEL)
    LOCAL_LLM_API_KEY      only if the server wants one (vLLM's --api-key)
    LOCAL_LLM_TIMEOUT      seconds to wait for an answer (default 600; a big model on a busy GPU is slow)
"""

import json
import os
import re
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_TIMEOUT = 600

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


class LLMError(RuntimeError):
    pass


def strip_reasoning(text: str) -> str:
    """Drops <think>...</think> blocks (Qwen3, DeepSeek-R1 and similar write
    their reasoning there) and surrounding whitespace."""
    return _THINK.sub("", text or "").strip()


def strip_fences(text: str) -> str:
    """The text inside a single ```...``` block, if the whole answer is one;
    otherwise the text as it is."""
    match = _FENCE.match(text)
    return match.group(1) if match else text


def parse_json(text: str):
    """The JSON object or array in a model's answer, tolerating reasoning
    blocks, code fences and a sentence before or after it."""
    text = strip_fences(strip_reasoning(text))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    pairs = [("{", "}"), ("[", "]")]
    pairs.sort(key=lambda pair: text.find(pair[0]) if pair[0] in text else len(text))  # whichever opens first
    for open_char, close_char in pairs:
        start, end = text.find(open_char), text.rfind(close_char)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"The model's answer wasn't valid JSON: {text[:200]!r}")


@dataclass
class LocalLLM:
    base_url: str
    model: str
    api_key: str = ""
    timeout: float = DEFAULT_TIMEOUT
    session: requests.Session | None = None

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        self.session = self.session or requests.Session()
        self._structured_ok = True  # flips off if the server rejects response_format

    @property
    def label(self) -> str:
        return f"{self.model} at {self.base_url}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def chat(
        self,
        system: str,
        user: str,
        schema: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        """The model's answer as text (reasoning blocks removed). With
        `schema`, asks the server to hold the answer to that JSON schema;
        a server that doesn't support it gets the plain request instead,
        and parse_json copes with the result."""
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if schema is not None and self._structured_ok:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema, "strict": True},
            }
        resp = self._post(body)
        if resp.status_code in (400, 422) and "response_format" in body:
            self._structured_ok = False
            body.pop("response_format")
            resp = self._post(body)
        if resp.status_code != 200:
            raise LLMError(f"{self.label} answered HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{self.label} sent an answer in an unexpected shape: {resp.text[:300]}") from e
        return strip_reasoning(content or "")

    def _post(self, body: dict) -> requests.Response:
        try:
            return self.session.post(
                f"{self.base_url}/chat/completions", json=body, headers=self._headers(), timeout=self.timeout
            )
        except requests.RequestException as e:
            raise LLMError(
                f"Couldn't reach the local model server at {self.base_url} ({e.__class__.__name__}). "
                "Is Ollama/vLLM running, and is LOCAL_LLM_URL right?"
            ) from e

    def chat_json(self, system: str, user: str, schema: dict, **kwargs):
        return parse_json(self.chat(system, user, schema=schema, **kwargs))

    def models(self) -> list[str]:
        """Model names the server offers (GET /models)."""
        try:
            resp = self.session.get(f"{self.base_url}/models", headers=self._headers(), timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise LLMError(
                f"Couldn't reach the local model server at {self.base_url} ({e.__class__.__name__}). "
                "Is Ollama/vLLM running, and is LOCAL_LLM_URL right?"
            ) from e
        return [m.get("id", "") for m in resp.json().get("data", [])]


def from_env(purpose: str = "discovery") -> LocalLLM:
    """The configured local model; `purpose` "codegen" uses
    LOCAL_CODEGEN_MODEL when it's set."""
    url = os.environ.get("LOCAL_LLM_URL", "").strip()
    model = os.environ.get("LOCAL_LLM_MODEL", "").strip()
    if purpose == "codegen":
        model = os.environ.get("LOCAL_CODEGEN_MODEL", "").strip() or model
    if not url or not model:
        raise LLMError("Local models need LOCAL_LLM_URL and LOCAL_LLM_MODEL in .env (see docs/local-discovery.md).")
    timeout = float(os.environ.get("LOCAL_LLM_TIMEOUT", "") or DEFAULT_TIMEOUT)
    return LocalLLM(url, model, os.environ.get("LOCAL_LLM_API_KEY", "").strip(), timeout)
