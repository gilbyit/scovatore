"""Persistenza SQLite: piani, esecuzioni, annunci visti e verdetti."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    hunt TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (hunt, plan_hash)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hunt TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS items (
    hunt TEXT NOT NULL,
    legacy_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    marketplace TEXT,
    title TEXT,
    url TEXT,
    price REAL,
    shipping REAL,
    total REAL,
    currency TEXT,
    condition TEXT,
    country TEXT,
    seller TEXT,
    is_auction INTEGER,
    end_date TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    screen_verdict TEXT,          -- si | forse | no | saltato
    screen_reason TEXT,
    verify_hash TEXT,
    verify_total REAL,            -- prezzo totale al momento della verifica
    verdict TEXT,                 -- conforme | non_conforme | incerto
    score INTEGER,
    verify_json TEXT,
    notified INTEGER DEFAULT 0,
    image TEXT,
    hidden INTEGER DEFAULT 0,     -- 1 = eliminato a mano: la pipeline lo ignora
    manual_verdict TEXT,          -- esito deciso dall'operatore, prevale su quello del modello
    manual_note TEXT,
    manual_at TEXT,
    PRIMARY KEY (hunt, legacy_id)
);
CREATE INDEX IF NOT EXISTS idx_items_score ON items(hunt, score DESC);
"""

# Colonne aggiunte dopo la prima versione: (tabella, colonna, tipo). Applicate all'avvio se mancano.
MIGRATIONS = [
    ("items", "image", "TEXT"),
    ("items", "hidden", "INTEGER DEFAULT 0"),
    ("items", "manual_verdict", "TEXT"),
    ("items", "manual_note", "TEXT"),
    ("items", "manual_at", "TEXT"),
]

MANUAL_VERDICTS = ("conforme", "incerto", "non_conforme")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- piani --------------------------------------------------------
    def get_plan(self, hunt: str, plan_hash: str) -> dict | None:
        r = self.conn.execute("SELECT plan_json FROM plans WHERE hunt=? AND plan_hash=?",
                              (hunt, plan_hash)).fetchone()
        return json.loads(r["plan_json"]) if r else None

    def save_plan(self, hunt: str, plan_hash: str, plan: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,?)",
                          (hunt, plan_hash, json.dumps(plan, ensure_ascii=False), now()))
        self.conn.commit()

    # --- esecuzioni ---------------------------------------------------
    def start_run(self, hunt: str) -> int:
        cur = self.conn.execute("INSERT INTO runs (hunt, started_at) VALUES (?,?)", (hunt, now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict, error: str | None = None) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, stats_json=?, error=? WHERE id=?",
                          (now(), json.dumps(stats, ensure_ascii=False), error, run_id))
        self.conn.commit()

    def last_run_at(self, hunt: str) -> str | None:
        r = self.conn.execute("SELECT MAX(started_at) AS t FROM runs WHERE hunt=? AND error IS NULL",
                              (hunt,)).fetchone()
        return r["t"] if r else None

    def ebay_calls_today(self) -> int:
        """Chiamate eBay dal giorno corrente (UTC), sommando le statistiche delle esecuzioni."""
        today = now()[:10]
        r = self.conn.execute(
            """SELECT COALESCE(SUM(json_extract(stats_json, '$.chiamate_ebay')), 0) AS n
               FROM runs WHERE substr(started_at, 1, 10) = ?""", (today,)).fetchone()
        return int(r["n"] or 0)

    # --- annunci ------------------------------------------------------
    def get_item(self, hunt: str, legacy_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE hunt=? AND legacy_id=?",
                                 (hunt, legacy_id)).fetchone()

    def upsert_seen(self, hunt: str, l) -> bool:
        """Registra l'annuncio. Ritorna True se e' nuovo."""
        t = now()
        existing = self.get_item(hunt, l.legacy_id)
        if existing is None:
            self.conn.execute(
                """INSERT INTO items (hunt, legacy_id, item_id, marketplace, title, url, price, shipping,
                   total, currency, condition, country, seller, is_auction, end_date, first_seen, last_seen, image)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (hunt, l.legacy_id, l.item_id, l.marketplace, l.title, l.url, l.price, l.shipping,
                 l.total, l.currency, l.condition, l.country, l.seller, int(l.is_auction), l.end_date, t, t,
                 l.image))
        else:
            self.conn.execute(
                """UPDATE items SET title=?, url=?, price=?, shipping=?, total=?, last_seen=?, end_date=?,
                   image=COALESCE(NULLIF(?, ''), image) WHERE hunt=? AND legacy_id=?""",
                (l.title, l.url, l.price, l.shipping, l.total, t, l.end_date, l.image, hunt, l.legacy_id))
        self.conn.commit()
        return existing is None

    def set_screen(self, hunt: str, legacy_id: str, verdict: str, reason: str) -> None:
        self.conn.execute("UPDATE items SET screen_verdict=?, screen_reason=? WHERE hunt=? AND legacy_id=?",
                          (verdict, reason, hunt, legacy_id))
        self.conn.commit()

    def set_verify(self, hunt: str, legacy_id: str, verify_hash: str, total: float, result: dict) -> None:
        score = result.get("punteggio")
        try:
            score = int(score)
        except (TypeError, ValueError):
            score = None
        self.conn.execute(
            """UPDATE items SET verify_hash=?, verify_total=?, verdict=?, score=?, verify_json=?
               WHERE hunt=? AND legacy_id=?""",
            (verify_hash, total, result.get("esito"), score, json.dumps(result, ensure_ascii=False),
             hunt, legacy_id))
        self.conn.commit()

    def mark_notified(self, hunt: str, legacy_id: str) -> None:
        self.conn.execute("UPDATE items SET notified=1 WHERE hunt=? AND legacy_id=?", (hunt, legacy_id))
        self.conn.commit()

    # --- interventi manuali (interfaccia web) ------------------------
    def _update_many(self, hunt: str, ids: list[str], sql_set: str, args: tuple) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur = self.conn.execute(f"UPDATE items SET {sql_set} WHERE hunt=? AND legacy_id IN ({marks})",
                                (*args, hunt, *ids))
        self.conn.commit()
        return cur.rowcount

    def set_hidden(self, hunt: str, ids: list[str], hidden: bool) -> int:
        return self._update_many(hunt, ids, "hidden=?", (int(hidden),))

    def set_manual(self, hunt: str, ids: list[str], verdict: str | None, note: str = "") -> int:
        """Esito dell'operatore; None lo toglie e torna a valere quello del modello."""
        if verdict is not None and verdict not in MANUAL_VERDICTS:
            raise ValueError(f"esito non valido: {verdict}")
        if verdict is None:
            return self._update_many(hunt, ids, "manual_verdict=NULL, manual_note=NULL, manual_at=NULL", ())
        return self._update_many(hunt, ids, "manual_verdict=?, manual_note=?, manual_at=?",
                                 (verdict, note or None, now()))

    def requeue(self, hunt: str, ids: list[str]) -> int:
        """Rimette in coda per la verifica Groq al prossimo giro (anche se la scrematura aveva detto no)."""
        return self._update_many(
            hunt, ids,
            """screen_verdict='si', screen_reason='rimesso in verifica a mano', verify_hash=NULL,
               verify_total=NULL, verdict=NULL, score=NULL, verify_json=NULL,
               manual_verdict=NULL, manual_note=NULL, manual_at=NULL""", ())

    def results(self, hunt: str, min_score: int = 0, limit: int = 50, include_unverified: bool = False):
        where = "hunt=? AND COALESCE(hidden, 0)=0"
        args: list = [hunt]
        if not include_unverified:
            where += " AND score IS NOT NULL AND score >= ?"
            args.append(min_score)
        return self.conn.execute(
            f"""SELECT * FROM items WHERE {where}
                ORDER BY score IS NULL, score DESC, total ASC LIMIT ?""", (*args, limit)).fetchall()
