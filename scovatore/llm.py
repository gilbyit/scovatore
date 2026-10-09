"""Client OpenAI-compatible usato sia per Palantir sia per Groq."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque

import httpx

from .config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)  # stima prudente per testo misto it/en/de


_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str):
    """Estrae il primo oggetto JSON valido da una risposta, anche se sporca."""
    if not text:
        raise LLMError("risposta vuota dal modello")
    text = _THINK.sub("", text).strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise LLMError(f"nessun JSON valido nella risposta: {text[:200]!r}")


def failed_generation(text: str) -> tuple[bool, str]:
    """(e' un json_validate_failed di Groq?, testo che il modello aveva generato)."""
    try:
        err = (json.loads(text) or {}).get("error") or {}
    except (ValueError, AttributeError):
        return False, ""
    if not isinstance(err, dict) or err.get("code") != "json_validate_failed":
        return False, ""
    return True, str(err.get("failed_generation") or "")


class TokenBucket:
    """Limitatore a finestra mobile di 60 s sui token stimati."""

    def __init__(self, tpm: int):
        self.tpm = tpm
        self.events: deque[tuple[float, int]] = deque()

    def wait(self, tokens: int) -> None:
        if self.tpm <= 0:
            return
        tokens = min(tokens, self.tpm)
        while True:
            now = time.monotonic()
            while self.events and now - self.events[0][0] > 60:
                self.events.popleft()
            used = sum(t for _, t in self.events)
            if used + tokens <= self.tpm:
                self.events.append((now, tokens))
                return
            sleep = 60 - (now - self.events[0][0]) + 0.5
            log.info("limite token/minuto: attendo %.0f s", sleep)
            time.sleep(max(1.0, sleep))


class LLMClient:
    def __init__(self, cfg: LLMConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.bucket = TokenBucket(cfg.tpm_limit)
        self.http = httpx.Client(timeout=cfg.timeout, transport=transport,
                                 headers={"User-Agent": "scovatore/0.1"})
        self._json_mode = cfg.json_mode
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def close(self) -> None:
        self.http.close()

    def chat_json(self, system: str, user: str, max_tokens: int | None = None):
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        if self.cfg.reasoning_effort:
            body["reasoning_effort"] = self.cfg.reasoning_effort
        headers = {}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        est = estimate_tokens(system + user) + body["max_tokens"]
        last_err = ""
        call_no_json = False        # solo per questa chiamata: dopo due JSON rifiutati si prova senza response_format
        for attempt in range(4):
            if self._json_mode and not call_no_json:
                body["response_format"] = {"type": "json_object"}
            else:
                body.pop("response_format", None)
            self.bucket.wait(est)
            log.debug("%s: richiesta %s (~%d token stimati, tentativo %d)", self.cfg.name, self.cfg.model,
                      est, attempt + 1)
            t0 = time.monotonic()
            try:
                r = self.http.post(f"{self.cfg.base_url}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning("%s: errore di rete dopo %.1f s (%s), riprovo fra %d s", self.cfg.name,
                            time.monotonic() - t0, last_err, 3 * (attempt + 1))
                time.sleep(3 * (attempt + 1))
                continue
            dt = time.monotonic() - t0
            self.calls += 1
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after") or 20)
                log.warning("%s: 429, attendo %.0f s", self.cfg.name, wait)
                time.sleep(min(wait, 120))
                continue
            bad_json, generated = failed_generation(r.text) if r.status_code == 400 else (False, "")
            if bad_json:
                # Groq ha rifiutato un JSON del modello. Spesso e' cosi' lungo da essere stato troncato da max_tokens
                # (nei modelli che ragionano i token di ragionamento contano): se il testo e' comunque leggibile
                # lo si recupera, altrimenti si riprova con piu' spazio e, dalla seconda volta, senza json mode.
                try:
                    obj = extract_json(generated)
                    log.info("%s: JSON rifiutato da Groq ma leggibile, lo recupero", self.cfg.name)
                    return obj
                except LLMError:
                    pass
                last_err = f"{r.status_code} json_validate_failed (risposta probabilmente troncata)"
                body["max_tokens"] = min(int(body["max_tokens"] * 1.6) + 200, 6000)
                est = estimate_tokens(system + user) + body["max_tokens"]
                call_no_json = call_no_json or attempt >= 1
                log.warning("%s: JSON non valido (%s), riprovo con max_tokens %d%s", self.cfg.name,
                            generated[-60:].replace("\n", " ") or "?", body["max_tokens"],
                            " senza json mode" if call_no_json else "")
                continue
            if r.status_code == 400 and self._json_mode and "response_format" in r.text:
                log.warning("%s: response_format non supportato, lo disattivo", self.cfg.name)
                self._json_mode = False
                continue
            if r.status_code >= 500:
                last_err = f"{r.status_code}: {r.text[:200]}"
                log.warning("%s: errore %d dopo %.1f s, riprovo", self.cfg.name, r.status_code, dt)
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"{self.cfg.name} {r.status_code}: {r.text[:400]}")
            data = r.json()
            usage = data.get("usage") or {}
            t_in = int(usage.get("prompt_tokens") or 0)
            t_out = int(usage.get("completion_tokens") or 0)
            self.tokens_in += t_in
            self.tokens_out += t_out
            log.debug("%s: risposta in %.1f s, token %d in / %d out", self.cfg.name, dt, t_in, t_out)
            if dt > 60:
                log.info("%s: risposta lenta, %.0f s (token %d in / %d out)", self.cfg.name, dt, t_in, t_out)
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            try:
                return extract_json(content)
            except LLMError as exc:
                last_err = str(exc)
                log.warning("%s: JSON non valido, riprovo (%s)", self.cfg.name, exc)
                continue
        raise LLMError(f"{self.cfg.name}: tentativi esauriti ({last_err})")
