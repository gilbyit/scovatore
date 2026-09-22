"""La pipeline di una caccia: piano -> ricerca -> filtri -> scrematura -> verifica -> notifica."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import prompts
from .config import Config
from .db import DB
from .ebay import EbayClient, EbayError, Listing
from .hunt import Hunt
from .llm import LLMClient, LLMError
from .notify import notify

log = logging.getLogger(__name__)

SCREEN_ORDER = {"si": 0, "forse": 1, "saltato": 1}


@dataclass
class RunStats:
    query: int = 0
    trovati: int = 0
    unici: int = 0
    scartati_filtri: int = 0
    nuovi: int = 0
    scremati: int = 0
    scremati_no: int = 0
    verificati: int = 0
    conformi: int = 0
    notificati: int = 0
    chiamate_ebay: int = 0
    token_palantir: int = 0
    token_groq: int = 0
    errori: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. Piano
# ---------------------------------------------------------------------------
def normalize_plan(raw: dict, languages: list[str], per_lang: int) -> dict:
    queries = raw.get("query") or {}
    if isinstance(queries, list):  # il modello ha ignorato la struttura per lingua
        queries = {"en": queries}
    clean: dict[str, list[str]] = {}
    for lang in languages:
        seen: list[str] = []
        for q in queries.get(lang) or []:
            q = re.sub(r"\s+", " ", str(q)).strip().strip('"')
            if q and q.lower() not in (s.lower() for s in seen):
                seen.append(q)
        clean[lang] = seen[:per_lang]
    return {
        "elementi_chiave": [str(x) for x in raw.get("elementi_chiave") or []],
        "query": clean,
        "parole_escluse": [str(x).lower() for x in raw.get("parole_escluse") or []][:8],
        "requisiti_base": [str(x) for x in raw.get("requisiti_base") or []][:5],
    }


def get_plan(hunt: Hunt, db: DB, palantir: LLMClient | None, force: bool = False) -> dict:
    if not force:
        cached = db.get_plan(hunt.nome, hunt.plan_hash)
        if cached:
            return cached
    if palantir is None:
        raise LLMError("serve Palantir per generare il piano (o usa solo query_extra)")
    langs = hunt.languages()
    raw = palantir.chat_json(prompts.PLAN_SYSTEM,
                             prompts.plan_user(hunt.ricerca, langs, hunt.max_query_per_lingua))
    if not isinstance(raw, dict):
        raise LLMError("il piano di Palantir non e' un oggetto JSON")
    plan = normalize_plan(raw, langs, hunt.max_query_per_lingua)
    if not any(plan["query"].values()) and not hunt.query_extra:
        raise LLMError("Palantir non ha prodotto query utilizzabili")
    db.save_plan(hunt.nome, hunt.plan_hash, plan)
    return plan


def queries_for(marketplace_lang: str, plan: dict, hunt: Hunt) -> list[str]:
    out: list[str] = []
    for q in list(hunt.query_extra) + plan["query"].get(marketplace_lang, []) + plan["query"].get("en", []):
        if q.lower() not in (x.lower() for x in out):
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# 2-3. Ricerca e filtri locali
# ---------------------------------------------------------------------------
def local_reject_reason(l: Listing, hunt: Hunt, excluded: list[str]) -> str | None:
    p = hunt.ebay
    if l.currency and l.currency != p.valuta:
        return f"valuta {l.currency}"
    if l.shipping is None and p.spedizione_ignota == "scarta":
        return "spedizione ignota"
    if p.prezzo_max is not None:
        cost = l.total if p.spedizione_inclusa else l.price
        if cost > p.prezzo_max:
            return f"totale {cost:.2f} oltre budget"
    title = l.title.lower()
    for w in excluded:
        if w and re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", title):
            return f"parola esclusa '{w}'"
    if p.feedback_minimo is not None and l.seller_feedback_pct is not None \
            and l.seller_feedback_pct < p.feedback_minimo:
        return f"feedback {l.seller_feedback_pct}%"
    return None


def search_all(hunt: Hunt, plan: dict, ebay: EbayClient, stats: RunStats) -> list[Listing]:
    from .hunt import MARKETPLACE_LANG
    best: dict[str, Listing] = {}
    for mp in hunt.ebay.marketplaces:
        for q in queries_for(MARKETPLACE_LANG.get(mp, "en"), plan, hunt):
            stats.query += 1
            try:
                found = ebay.search(q, mp, hunt.ebay)
            except EbayError as exc:
                stats.errori.append(f"{mp} '{q}': {exc}")
                log.error("ricerca fallita %s '%s': %s", mp, q, exc)
                if "budget" in str(exc):
                    return list(best.values())
                continue
            log.info("%s '%s': %d risultati", mp, q, len(found))
            stats.trovati += len(found)
            for l in found:
                cur = best.get(l.legacy_id)
                if cur is None or l.total < cur.total:
                    best[l.legacy_id] = l
    stats.unici = len(best)
    return list(best.values())


# ---------------------------------------------------------------------------
# 4. Scrematura (Palantir, titoli in blocco)
# ---------------------------------------------------------------------------
def screen(hunt: Hunt, plan: dict, items: list[Listing], palantir: LLMClient, db: DB,
           batch_size: int, stats: RunStats) -> None:
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        ids = {str(n + 1): l for n, l in enumerate(batch)}   # id corti: meno token, meno errori
        payload = [{"id": k, "title": l.title, "price": f"{l.total:.2f} {l.currency}",
                    "condition": l.condition} for k, l in ids.items()]
        try:
            res = palantir.chat_json(prompts.SCREEN_SYSTEM,
                                     prompts.screen_user(hunt.ricerca, plan["requisiti_base"], payload),
                                     max_tokens=60 + 40 * len(batch))
            rows = res.get("valutazioni", []) if isinstance(res, dict) else res
        except LLMError as exc:
            stats.errori.append(f"scrematura: {exc}")
            log.error("scrematura fallita, il blocco passa come 'forse': %s", exc)
            rows = []
        got: dict[str, tuple[str, str]] = {}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            v = str(r.get("esito", "")).lower().strip()
            v = {"sì": "si", "yes": "si", "maybe": "forse", "non": "no"}.get(v, v)
            if v in ("si", "forse", "no"):
                got[str(r.get("id"))] = (v, str(r.get("motivo", ""))[:200])
        for k, l in ids.items():
            verdict, reason = got.get(k, ("forse", "non valutato dal modello"))
            db.set_screen(hunt.nome, l.legacy_id, verdict, reason)
            stats.scremati += 1
            if verdict == "no":
                stats.scremati_no += 1


# ---------------------------------------------------------------------------
# 5. Verifica avanzata (Groq, un annuncio alla volta)
# ---------------------------------------------------------------------------
def listing_payload(l: Listing) -> dict:
    return {
        "titolo": l.title,
        "prezzo": l.price, "spedizione": l.shipping, "totale": l.total, "valuta": l.currency,
        "asta": l.is_auction,
        "condizione": l.condition, "note_condizione": l.condition_description,
        "specifiche": l.aspects,
        "descrizione": l.description,
        "paese": l.country,
        "venditore_feedback_pct": l.seller_feedback_pct,
        "venditore_feedback_n": l.seller_feedback_score,
    }


def needs_verify(row, hunt: Hunt, total: float) -> bool:
    if row is None or row["verify_hash"] != hunt.verify_hash or row["verify_total"] is None:
        return True
    # riverifica se il prezzo e' cambiato di oltre il 5% (aste, ribassi)
    return abs(total - row["verify_total"]) > max(1.0, 0.05 * row["verify_total"])


def verify(hunt: Hunt, items: list[Listing], ebay: EbayClient, groq: LLMClient, db: DB,
           cfg: Config, stats: RunStats) -> None:
    ref = hunt.reference_text()
    for l in items:
        # il confronto per la riverifica si fa sempre sul totale della ricerca, che e' quello
        # che rivedremo al prossimo giro (il dettaglio puo' riportare una spedizione diversa)
        summary_total = l.total
        try:
            ebay.get_item(l, cfg.description_max_chars)
        except EbayError as exc:
            log.warning("dettaglio non disponibile per %s: %s", l.legacy_id, exc)
        try:
            res = groq.chat_json(prompts.VERIFY_SYSTEM,
                                 prompts.verify_user(hunt.ricerca, hunt.requisiti_avanzati, ref,
                                                     listing_payload(l)))
        except LLMError as exc:
            stats.errori.append(f"verifica {l.legacy_id}: {exc}")
            log.error("verifica fallita per %s: %s", l.legacy_id, exc)
            if "tentativi esauriti" in str(exc):
                break   # rate limit persistente: inutile insistere in questo giro
            continue
        if not isinstance(res, dict):
            continue
        db.set_verify(hunt.nome, l.legacy_id, hunt.verify_hash, summary_total, res)
        stats.verificati += 1
        if res.get("esito") == "conforme":
            stats.conformi += 1
        log.info("verificato %s: %s %s", l.legacy_id, res.get("esito"), res.get("punteggio"))


# ---------------------------------------------------------------------------
# Orchestrazione
# ---------------------------------------------------------------------------
def run_hunt(hunt: Hunt, cfg: Config, db: DB, ebay: EbayClient, palantir: LLMClient | None,
             groq: LLMClient | None, force_plan: bool = False) -> RunStats:
    stats = RunStats()
    run_id = db.start_run(hunt.nome)
    calls_before = ebay.calls
    error = None
    try:
        plan = get_plan(hunt, db, palantir, force=force_plan)
        excluded = [w.lower() for w in hunt.parole_escluse] + plan["parole_escluse"]

        listings = search_all(hunt, plan, ebay, stats)
        kept: list[Listing] = []
        for l in listings:
            reason = local_reject_reason(l, hunt, excluded)
            if reason:
                stats.scartati_filtri += 1
                log.debug("scartato %s: %s", l.legacy_id, reason)
                continue
            if db.upsert_seen(hunt.nome, l):
                stats.nuovi += 1
            kept.append(l)

        # scrematura solo per chi non e' mai stato scremato
        to_screen = [l for l in kept if (db.get_item(hunt.nome, l.legacy_id)["screen_verdict"] is None)]
        if to_screen:
            if hunt.screening and palantir is not None:
                screen(hunt, plan, to_screen, palantir, db, cfg.screen_batch_size, stats)
            else:
                for l in to_screen:
                    db.set_screen(hunt.nome, l.legacy_id, "saltato", "")

        # candidati alla verifica: si prima di forse, poi dal piu' economico
        cands = []
        for l in kept:
            row = db.get_item(hunt.nome, l.legacy_id)
            if row["screen_verdict"] == "no":
                continue
            if needs_verify(row, hunt, l.total):
                cands.append((SCREEN_ORDER.get(row["screen_verdict"], 2), l.total, l))
        cands.sort(key=lambda t: (t[0], t[1]))
        todo = [c[2] for c in cands[: cfg.max_verify_per_run]]
        if len(cands) > len(todo):
            log.info("%d candidati rimandati al prossimo giro (limite %d)", len(cands) - len(todo),
                     cfg.max_verify_per_run)

        if groq is not None and todo:
            verify(hunt, todo, ebay, groq, db, cfg, stats)
        elif todo:
            log.warning("GROQ_API_KEY assente: verifica avanzata saltata per %d annunci", len(todo))

        stats.notificati = notify(hunt, db, cfg)
    except Exception as exc:  # l'errore finisce nel DB e nel log, poi risale
        error = f"{type(exc).__name__}: {exc}"
        stats.errori.append(error)
        raise
    finally:
        stats.chiamate_ebay = ebay.calls - calls_before
        if palantir:
            stats.token_palantir = palantir.tokens_in + palantir.tokens_out
        if groq:
            stats.token_groq = groq.tokens_in + groq.tokens_out
        db.finish_run(run_id, stats.__dict__, error)
    return stats
