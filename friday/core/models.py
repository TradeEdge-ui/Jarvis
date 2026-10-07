"""Model providers behind one interface, plus a router. Any provider is replaceable.

Neutral message format used throughout the agent:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments": dict}]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""


class ModelError(RuntimeError):
    pass


def _loads_args(a) -> dict:
    if isinstance(a, dict):
        return a
    if isinstance(a, str) and a.strip():
        try:
            v = json.loads(a)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {}


def _salvage_inline_call(text: str, tool_names: set[str]) -> ToolCall | None:
    """Some small models print the call as JSON in the content instead of using the tool-call channel."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    if not t.startswith("{"):
        return None
    try:
        obj = json.loads(t)
    except ValueError:
        return None
    name = obj.get("name") or (obj.get("function") or {}).get("name")
    args = obj.get("arguments") or obj.get("parameters") or (obj.get("function") or {}).get("arguments") or {}
    if name in tool_names:
        return ToolCall(f"call_{uuid.uuid4().hex[:8]}", name, _loads_args(args))
    return None


class ModelProvider:
    name = "base"
    model = ""

    def chat(self, messages: list[dict], tools: list[dict] | None, system: str = "") -> ModelResponse:  # pragma: no cover
        raise NotImplementedError


class OllamaProvider(ModelProvider):
    """Ollama native /api/chat with tool calling."""
    name = "ollama"

    def __init__(self, base_url: str, model: str, client: httpx.Client | None = None, timeout: float = 120.0):
        self.base_url, self.model = base_url.rstrip("/"), model
        self.client = client or httpx.Client(timeout=timeout)

    @staticmethod
    def _convert(messages: list[dict], system: str) -> list[dict]:
        out = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content") or "",
                            "tool_calls": [{"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                           for tc in m["tool_calls"]]})
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_name": m.get("name", ""), "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def chat(self, messages, tools, system=""):
        body = {"model": self.model, "messages": self._convert(messages, system), "stream": False,
                "options": {"temperature": 0.2}}
        if tools:
            body["tools"] = tools
        try:
            r = self.client.post(f"{self.base_url}/api/chat", json=body)
            r.raise_for_status()
            msg = r.json()["message"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise ModelError(f"Ollama request failed: {e}") from e
        calls = [ToolCall(f"call_{uuid.uuid4().hex[:8]}", tc["function"]["name"], _loads_args(tc["function"].get("arguments")))
                 for tc in (msg.get("tool_calls") or [])]
        text = msg.get("content") or ""
        if not calls and tools:
            names = {t["function"]["name"] for t in tools}
            salvaged = _salvage_inline_call(text, names)
            if salvaged:
                calls, text = [salvaged], ""
        return ModelResponse(text, calls, self.model, self.name)


class OpenAICompatProvider(ModelProvider):
    """Any OpenAI-compatible /chat/completions server (llama.cpp, LM Studio, vLLM, hosted APIs, Ollama's /v1)."""
    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "", client: httpx.Client | None = None, timeout: float = 120.0):
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.client = client or httpx.Client(timeout=timeout)

    @staticmethod
    def _convert(messages: list[dict], system: str) -> list[dict]:
        out = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content") or None,
                            "tool_calls": [{"id": tc["id"], "type": "function",
                                            "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                                           for tc in m["tool_calls"]]})
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def chat(self, messages, tools, system=""):
        body = {"model": self.model, "messages": self._convert(messages, system), "temperature": 0.2}
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise ModelError(f"OpenAI-compatible request failed: {e}") from e
        calls = [ToolCall(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", tc["function"]["name"], _loads_args(tc["function"].get("arguments")))
                 for tc in (msg.get("tool_calls") or [])]
        return ModelResponse(msg.get("content") or "", calls, self.model, self.name)


# ---------------------------------------------------------------- task classification + routing
_CODE = re.compile(r"```|traceback|stack ?trace|\bdef \w+\(|\bfunction\b|\bscript\b|\bdebug|\bregex\b|\bsql\b|\bpowershell\b|\bpython\b|\bexception\b|\bcompile", re.I)
_COMPLEX = re.compile(r"\b(why|how|compare|plan|analy[sz]e|strategy|decide|decision|recommend|explain|research|evaluate|should i|pros and cons)\b", re.I)


def classify_task(text: str, has_image: bool = False) -> str:
    if has_image:
        return "vision"
    if _CODE.search(text):
        return "coding"
    if len(text.split()) <= 8 and not _COMPLEX.search(text):
        return "simple"
    return "reasoning"


class ModelRouter:
    """Maps a task kind (simple / reasoning / coding / vision) to a provider+model; falls back to the offline rules
    planner when no language model is reachable."""

    def __init__(self, svc, http: httpx.Client | None = None):
        self.svc = svc
        self.cfg = svc.config
        self.http = http
        self._cache: dict[tuple[str, str], ModelProvider] = {}
        self._probe: tuple[float, dict] | None = None

    def _client(self) -> httpx.Client:
        return self.http or httpx.Client(timeout=120.0)

    def probe_ollama(self, force: bool = False) -> dict:
        now = time.monotonic()
        if self._probe and not force and now - self._probe[0] < 20:
            return self._probe[1]
        info = {"reachable": False, "models": [], "error": ""}
        try:
            r = (self.http or httpx).get(f"{self.cfg.ollama_url}/api/tags", timeout=1.5)
            r.raise_for_status()
            info.update(reachable=True, models=[m["name"] for m in r.json().get("models", [])])
        except Exception as e:
            info["error"] = f"{e.__class__.__name__}"
        self._probe = (now, info)
        return info

    def _build(self, provider: str, model: str) -> ModelProvider:
        key = (provider, model)
        if key not in self._cache:
            if provider == "ollama":
                self._cache[key] = OllamaProvider(self.cfg.ollama_url, model, self._client())
            elif provider == "openai":
                self._cache[key] = OpenAICompatProvider(self.cfg.openai_base_url, model, self.cfg.openai_api_key, self._client())
            else:
                from friday.core.rules import RulesProvider
                self._cache[key] = RulesProvider(self.svc)
        return self._cache[key]

    def backend(self) -> dict:
        """Describe what will actually answer, and why."""
        mode = self.cfg.model_provider
        rules = {"provider": "rules", "model": "offline-rules", "degraded": True}
        if mode == "rules":
            return {**rules, "mode": mode, "reason": "configured to use the offline rules planner"}
        if mode == "openai":
            if self.cfg.openai_base_url and self.cfg.openai_model:
                return {"provider": "openai", "model": self.cfg.openai_model, "degraded": False, "mode": mode, "reason": ""}
            return {**rules, "mode": mode, "reason": "FRIDAY_OPENAI_BASE_URL / FRIDAY_OPENAI_MODEL not set"}
        info = self.probe_ollama()
        if not info["reachable"]:
            return {**rules, "mode": mode, "reason": f"Ollama not reachable at {self.cfg.ollama_url} ({info['error']})"}
        want = self.cfg.ollama_model
        have = info["models"]
        if want in have or any(m.split(":")[0] == want.split(":")[0] and ":" not in want for m in have):
            return {"provider": "ollama", "model": want, "degraded": False, "mode": mode, "reason": ""}
        if have:
            return {"provider": "ollama", "model": have[0], "degraded": False, "mode": mode,
                    "reason": f"configured model '{want}' is not installed; using '{have[0]}'"}
        return {**rules, "mode": mode, "reason": f"Ollama is running but has no models; run `ollama pull {want}`"}

    def provider_for(self, kind: str) -> tuple[ModelProvider, dict]:
        """Pick the provider+model for a task kind. Explicit FRIDAY_MODEL_<KIND> routes win while a real model
        backend is reachable; otherwise the active backend (or the offline rules planner) answers."""
        b = self.backend()
        if b["provider"] == "rules":
            return self._build("rules", ""), b
        p, m = self.cfg.routes.get(kind, (b["provider"], b["model"]))
        return self._build(p, m), {**b, "provider": p, "model": m, "kind": kind}
