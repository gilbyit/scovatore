"""Notifiche ntfy per i nuovi annunci sopra soglia (opzionali)."""
from __future__ import annotations

import json
import logging

import httpx

from .config import Config
from .db import DB
from .hunt import Hunt

log = logging.getLogger(__name__)


def notify(hunt: Hunt, db: DB, cfg: Config, transport: httpx.BaseTransport | None = None) -> int:
    if not cfg.ntfy_url:
        return 0
    rows = db.conn.execute(
        """SELECT * FROM items WHERE hunt=? AND notified=0 AND verdict='conforme' AND score>=?
           AND COALESCE(hidden, 0)=0 AND manual_verdict IS NULL
           ORDER BY score DESC""", (hunt.nome, cfg.notify_min_score)).fetchall()
    sent = 0
    headers_base = {}
    if cfg.ntfy_token:
        headers_base["Authorization"] = f"Bearer {cfg.ntfy_token}"
    with httpx.Client(timeout=15, transport=transport) as http:
        for r in rows:
            v = json.loads(r["verify_json"] or "{}")
            body = f"{r['total']:.2f} {r['currency']} | {r['score']}/100\n{v.get('sintesi', '')}"
            # titolo e link come query string: gli header HTTP non reggono caratteri non ASCII
            params = {"title": f"[{hunt.nome}] {r['title'][:80]}", "click": r["url"], "tags": "mag"}
            try:
                resp = http.post(cfg.ntfy_url, content=body.encode("utf-8"), params=params,
                                 headers=headers_base)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("notifica ntfy fallita: %s", exc)
                break
            db.mark_notified(hunt.nome, r["legacy_id"])
            sent += 1
    return sent
