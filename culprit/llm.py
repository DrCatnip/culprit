"""Nemotron on Nebius Token Factory (OpenAI-compatible API), plus an offline stand-in.

Which model does what:
  Nemotron 3 Nano  -> picks which files to read (cheap, fast)
  Nemotron 3 Super -> writes the reproduction test and the fixes (code + tool-style JSON)
  Nemotron 3 Ultra -> explains the root cause from the guilty diff (hardest reasoning)
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from culprit.config import MODEL_LABELS, PRICES_PER_MILLION
from culprit.security import redact, redact_data, safe_error


class LLMError(RuntimeError):
    pass


def validate_endpoint(base_url: str) -> None:
    """Credentials are sent only to the specified Nebius Token Factory endpoint."""
    try:
        url = urlsplit(base_url)
        valid = (url.scheme == "https" and url.hostname == "api.tokenfactory.nebius.com"
                 and url.port in (None, 443) and url.path.rstrip("/") == "/v1"
                 and not url.username and not url.password and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise LLMError("Token Factory must use https://api.tokenfactory.nebius.com/v1/.")


def is_nemotron(model: str) -> bool:
    return bool(re.fullmatch(r"nvidia/[A-Za-z0-9._/-]+", model, re.I)) and "nemotron" in model.lower()


def make_client(settings):
    from openai import OpenAI

    validate_endpoint(settings.base_url)
    if not settings.api_key:
        raise LLMError("NEBIUS_API_KEY is not set. Copy .env.example to .env and add your key.")
    return OpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=240, max_retries=2)


# ---------------------------------------------------------------- JSON parsing
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _match_brace(text: str, start: int) -> int | None:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def extract_json(text: str) -> dict:
    """Pull the JSON object out of a model reply (handles <think>, fences, chatter)."""
    if not text or not text.strip():
        raise ValueError("empty reply")
    cleaned = _THINK.sub("", text)
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>")[-1]
    candidates = [cleaned.strip()]
    candidates += [m.group(1) for m in _FENCE.finditer(cleaned)]
    starts = [i for i, ch in enumerate(cleaned) if ch == "{"][:60]
    for start in starts:
        end = _match_brace(cleaned, start)
        if end is not None:
            candidates.append(cleaned[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in the reply")


# ---------------------------------------------------------------- usage
@dataclass
class _Usage:
    model: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


class UsageTracker:
    def __init__(self) -> None:
        self._by_role: dict[str, _Usage] = {}
        self._lock = threading.Lock()

    def add_call(self, role: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        """One API call (also counts retries, because they cost tokens too)."""
        with self._lock:
            u = self._by_role.setdefault(role, _Usage(model=model))
            u.calls += 1
            u.prompt_tokens += prompt_tokens
            u.completion_tokens += completion_tokens

    def add_time(self, role: str, model: str, seconds: float) -> None:
        with self._lock:
            u = self._by_role.setdefault(role, _Usage(model=model))
            u.seconds += seconds

    def rows(self) -> list[dict]:
        out = []
        for role in ("nano", "super", "ultra"):
            u = self._by_role.get(role)
            if not u:
                continue
            pin, pout = PRICES_PER_MILLION.get(role, (0.0, 0.0))
            cost = u.prompt_tokens / 1e6 * pin + u.completion_tokens / 1e6 * pout
            out.append({
                "role": role, "label": MODEL_LABELS.get(role, role), "model": u.model, "calls": u.calls,
                "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                "seconds": round(u.seconds, 1), "cost_usd": round(cost, 5),
            })
        return out

    def total_cost(self) -> float:
        return round(sum(r["cost_usd"] for r in self.rows()), 5)


# ---------------------------------------------------------------- Token Factory
class TokenFactoryLLM:
    offline = False

    def __init__(self, settings, events, *, client=None) -> None:
        validate_endpoint(settings.base_url)
        if not settings.api_key:
            raise LLMError("NEBIUS_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.client = client if client is not None else make_client(settings)
        self.secrets = (settings.api_key,)
        self.models = dict(settings.models)
        self.events = events
        self.usage = UsageTracker()
        self.json_mode = True

    def close(self) -> None:
        self.client.close()

    def describe(self) -> dict:
        return {role: {"model": m, "label": MODEL_LABELS[role]} for role, m in self.models.items()}

    def chat_json(self, role: str, system: str, user: str, purpose: str,
                  temperature: float = 0.2, max_tokens: int = 12000) -> dict:
        from openai import BadRequestError

        model = self.models[role]
        if not is_nemotron(model):
            raise LLMError("Every model role must use an NVIDIA Nemotron model.")
        label = MODEL_LABELS[role]
        self.events.emit("model_started", role=role, model=model, label=label, purpose=purpose)
        messages = [{"role": "system", "content": redact(system, self.secrets)},
                    {"role": "user", "content": redact(user, self.secrets)}]
        if role == "nano" and max_tokens == 12000:
            max_tokens = 4000
        t0 = time.monotonic()
        last_error = "invalid JSON reply"
        use_json = self.json_mode
        for _ in range(4):
            kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
            if use_json:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                if use_json and any(word in str(exc).lower() for word in ("response_format", "json mode", "json_object")):
                    use_json = False
                    self.json_mode = False
                    last_error = safe_error(exc)
                    continue
                last_error = safe_error(exc)
                break
            except Exception as exc:
                last_error = safe_error(exc)
                break
            usage = getattr(resp, "usage", None)
            pt = getattr(usage, "prompt_tokens", 0) or 0
            ct = getattr(usage, "completion_tokens", 0) or 0
            self.usage.add_call(role, model, pt, ct)
            text = ""
            try:
                text = redact(resp.choices[0].message.content or "", self.secrets)
                data = extract_json(text)
            except (ValueError, IndexError, AttributeError, TypeError):
                last_error = "invalid JSON reply after the allowed retries"
                messages = messages + [
                    {"role": "assistant", "content": text[-6000:] or "(empty)"},
                    {"role": "user", "content": "That was not a valid JSON object. Reply again with ONLY the JSON object."},
                ]
                continue
            secs = time.monotonic() - t0
            self.usage.add_time(role, model, secs)
            self.events.emit("model_finished", role=role, model=model, label=label, purpose=purpose,
                             seconds=round(secs, 1), ok=True, prompt_tokens=pt, completion_tokens=ct)
            return redact_data(data, self.secrets)
        secs = time.monotonic() - t0
        self.usage.add_time(role, model, secs)
        self.events.emit("model_finished", role=role, model=model, label=label, purpose=purpose,
                         seconds=round(secs, 1), ok=False, error=last_error)
        raise LLMError(f"{label} ({purpose}) failed: {last_error}")


# ---------------------------------------------------------------- offline stand-in
class OfflineDemoLLM:
    """Scripted answers for the bundled demo repo, so you can build the UI and
    reports without credits. Never use it for the real demo video."""

    offline = True

    def __init__(self, repo: Path, events) -> None:
        from culprit.demo_repo import is_demo_repo

        if not is_demo_repo(repo):
            raise LLMError("Offline mode only works with the bundled demo repo (run: culprit demo-repo).")
        self.events = events
        self.usage = UsageTracker()

    def describe(self) -> dict:
        return {role: {"model": "offline-script", "label": f"{MODEL_LABELS[role]} (offline stand-in)"}
                for role in ("nano", "super", "ultra")}

    def chat_json(self, role: str, system: str, user: str, purpose: str,
                  temperature: float = 0.2, max_tokens: int = 0) -> dict:
        from culprit import demo_repo

        label = f"{MODEL_LABELS[role]} (offline)"
        self.events.emit("model_started", role=role, model="offline-script", label=label, purpose=purpose)
        time.sleep(0.15)
        if purpose == "pick_files":
            data = {"files": ["billing/money.py", "billing/invoice.py"]}
        elif purpose == "write_repro_test":
            data = {"test_code": demo_repo.DEMO_REPRO_TEST, "why": "Checks that ₹42.50 and ₹43.50 bills round half a rupee up."}
        elif purpose == "explain_culprit":
            data = dict(demo_repo.DEMO_EXPLANATION)
        elif purpose.startswith("fix_"):
            data = demo_repo.offline_fix(purpose, user)
        else:
            raise LLMError(f"offline stand-in has no answer for {purpose}")
        self.usage.add_call(role, "offline-script", 0, 0)
        self.usage.add_time(role, "offline-script", 0.15)
        self.events.emit("model_finished", role=role, model="offline-script", label=label, purpose=purpose,
                         seconds=0.2, ok=True, prompt_tokens=0, completion_tokens=0)
        return data
