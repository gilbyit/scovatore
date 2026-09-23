"""La pipeline di una caccia: piano -> ricerca -> filtri -> scrematura -> verifica -> notifica."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import prompts
from .config import Config
from .db import DB
from .ebay import EbayClient, EbayError, Listing, build_filter
from .hunt import MARKETPLACE_LANG, Hunt
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
    scartati_paese: int = 0
    ricerche_saltate: int = 0
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
    durate: dict[str, float] = field(default_factory=dict)


@contextmanager
def phase(stats: RunStats, name: str, hunt: str):
    """Logga inizio e fine di una fase con la durata, e la salva nelle statistiche."""
    log.info("[%s] %s: inizio", hunt, name)
    t0 = time.monotonic()
    try:
        yield
    finally:
        dt = round(time.monotonic() - t0, 1)
        stats.durate[name] = dt
        log.info("[%s] %s: fine in %.1f s", hunt, name, dt)


# ---------------------------------------------------------------------------
# 1. Piano
# ---------------------------------------------------------------------------
# Parole che da sole non identificano un oggetto: stato, condizione, riempitivi.
# Una query fatta solo di queste ("non funzionante", "defekt", "for parts") pesca in tutte
# le categorie di eBay e porta migliaia di risultati inutili.
GENERIC_WORDS = frozenset("""
guasto guasta guasti guaste non funzionante funzionanti funziona funzionano difettoso difettosa difettosi
per ricambi ricambio pezzi da riparare riparazione rotto rotta rotti usato usata usati testato testata
nuovo nuova ottimo stato perfetto lotto stock bundle kit set con senza e o il la lo i gli le un una di del
defekt defekte defekter defektes für bastler bastlerware ersatzteile ersatzteil nicht funktioniert
funktionsfähig funktionsfähige ungetestet reparatur gebraucht neu und mit ohne der die das ein eine
faulty broken for parts part spares spare repair not working untested as is used new and with without the a of
hs en panne pour pièces pieces pièce défectueux défectueuse ne fonctionne pas réparer occasion neuf et avec sans
averiado averiada averiados para piezas repuesto repuestos no funciona roto rota reparar usado nuevo y con sin
defect kapot onderdelen voor niet werkend werkt reparatie gebruikt nieuw en met zonder
uszkodzony uszkodzona uszkodzone na części czesci nie działa dziala sprawny naprawy używany uzywany nowy
""".split())


def is_generic_query(q: str) -> bool:
    """True se la query non contiene nessuna parola che indichi un oggetto."""
    words = [w for w in re.split(r"[^\w]+", q.lower()) if w]
    return not words or all(w in GENERIC_WORDS for w in words)


def normalize_plan(raw: dict, languages: list[str], per_lang: int) -> dict:
    queries = raw.get("query") or {}
    if isinstance(queries, list):  # il modello ha ignorato la struttura per lingua
        queries = {"en": queries}
    clean: dict[str, list[str]] = {}
    dropped: list[str] = []
    for lang in languages:
        seen: list[str] = []
        for q in queries.get(lang) or []:
            q = re.sub(r"\s+", " ", str(q)).strip().strip('"')
            if not q or q.lower() in (s.lower() for s in seen):
                continue
            if is_generic_query(q):
                dropped.append(f"{lang}: {q}")
                continue
            seen.append(q)
        clean[lang] = seen[:per_lang]
    if dropped:
        log.warning("query generiche scartate dal piano (solo parole di stato, nessun oggetto): %s", dropped)
    return {
        "elementi_chiave": [str(x) for x in raw.get("elementi_chiave") or []],
        "query": clean,
        "parole_escluse": [str(x).lower() for x in raw.get("parole_escluse") or []][:8],
        "requisiti_base": [str(x) for x in raw.get("requisiti_base") or []][:5],
        "scartate": dropped,
    }


def plan_note(hunt: Hunt) -> str:
    """Descrive al pianificatore i filtri che eBay applica gia', per non sprecare parole nelle query."""
    notes = []
    conds = set(hunt.ebay.condizioni)
    if conds and conds <= {7000}:
        notes.append("solo oggetti in condizione 'per ricambi o non funzionante' (le parole di stato "
                     "come guasto/defekt/for parts sono quindi superflue)")
    elif conds and 7000 not in conds and conds <= {1000, 1500}:
        notes.append("solo oggetti nuovi")
    elif conds and 7000 not in conds:
        notes.append("solo oggetti funzionanti (nuovi, usati o ricondizionati)")
    if hunt.ebay.categorie:
        notes.append("ricerca limitata a categorie specifiche")
    return "; ".join(notes)


def pick_planner(cfg: Config, palantir: LLMClient | None, groq: LLMClient | None) -> LLMClient | None:
    """Il modello che genera il piano, secondo SCOVATORE_PLAN_LLM, con ripiego sull'altro."""
    want, other = (groq, palantir) if cfg.plan_llm == "groq" else (palantir, groq)
    if want is not None:
        return want
    if other is not None:
        log.warning("SCOVATORE_PLAN_LLM=%s ma %s non e' configurato: il piano lo genera %s",
                    cfg.plan_llm, cfg.plan_llm, other.cfg.name)
    return other


def plan_key(hunt: Hunt, llm: LLMClient | None) -> str:
    """Chiave della cache del piano: cambia con la caccia, con chi lo genera e con il prompt."""
    who = f"{llm.cfg.name}:{llm.cfg.model}" if llm else "nessuno"
    src = f"{hunt.plan_hash}|{who}|{prompts.PLAN_SYSTEM}|{plan_note(hunt)}"
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def get_plan(hunt: Hunt, db: DB, planner: LLMClient | None, force: bool = False) -> dict:
    key = plan_key(hunt, planner)
    if not force:
        cached = db.get_plan(hunt.nome, key)
        if cached:
            log.info("[%s] piano in cache (%s, %s): %s", hunt.nome, key,
                     planner.cfg.name if planner else "?", _plan_summary(cached))
            return cached
    if planner is None:
        raise LLMError("serve Palantir o Groq per generare il piano (o usa solo query_extra)")
    note = plan_note(hunt)
    log.info("[%s] genero il piano con %s (%s) per le lingue %s%s", hunt.nome, planner.cfg.name,
             planner.cfg.model, ",".join(hunt.languages()), " (rigenerazione forzata)" if force else "")
    langs = hunt.languages()
    raw = planner.chat_json(prompts.PLAN_SYSTEM,
                            prompts.plan_user(hunt.ricerca, langs, hunt.max_query_per_lingua, note),
                            max_tokens=max(planner.cfg.max_tokens, 2000))
    if not isinstance(raw, dict):
        raise LLMError(f"il piano di {planner.cfg.name} non e' un oggetto JSON")
    plan = normalize_plan(raw, langs, hunt.max_query_per_lingua)
    plan["generato_da"] = f"{planner.cfg.name}:{planner.cfg.model}"
    if not any(plan["query"].values()) and not hunt.query_extra:
        raise LLMError(f"{planner.cfg.name} non ha prodotto query utilizzabili")
    db.save_plan(hunt.nome, key, plan)
    log.info("[%s] piano nuovo: %s", hunt.nome, _plan_summary(plan))
    for lang, qs in plan["query"].items():
        log.info("[%s]   %s: %s", hunt.nome, lang, qs)
    return plan


def _plan_summary(plan: dict) -> str:
    per_lang = ", ".join(f"{l}={len(q)}" for l, q in plan["query"].items())
    return f"query per lingua [{per_lang}], parole escluse {len(plan['parole_escluse'])}"


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
    allowed = p.allowed_countries()
    if allowed is not None and l.country and l.country.upper() not in allowed:
        return f"paese {l.country} fuori area"
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


def search_tasks(hunt: Hunt, plan: dict) -> list[tuple[str, str]]:
    """Coppie (marketplace, query) nell'ordine di esecuzione.

    Si procede a strati: prima la query n.1 di ogni marketplace, poi la n.2 e cosi' via.
    Se il tetto max_ricerche taglia, taglia le query meno importanti su tutti i
    marketplace, invece di lasciare scoperti gli ultimi della lista.
    """
    per_mp = {mp: queries_for(MARKETPLACE_LANG.get(mp, "en"), plan, hunt) for mp in hunt.ebay.marketplaces}
    depth = max((len(q) for q in per_mp.values()), default=0)
    return [(mp, qs[i]) for i in range(depth) for mp, qs in per_mp.items() if i < len(qs)]


def search_all(hunt: Hunt, plan: dict, ebay: EbayClient, stats: RunStats) -> list[Listing]:
    tasks = search_tasks(hunt, plan)
    cap = hunt.ebay.max_ricerche
    if len(tasks) > cap:
        stats.ricerche_saltate = len(tasks) - cap
        log.warning("[%s] %d ricerche previste, eseguo le prime %d (max_ricerche); saltate %d",
                    hunt.nome, len(tasks), cap, len(tasks) - cap)
        tasks = tasks[:cap]
    log.info("[%s] %d ricerche su %d marketplace (%s), filtro eBay: %s", hunt.nome, len(tasks),
             len(hunt.ebay.marketplaces), ",".join(m.removeprefix("EBAY_") for m in hunt.ebay.marketplaces),
             build_filter(hunt.ebay) or "(nessuno)")

    best: dict[str, Listing] = {}
    per_mp: Counter = Counter()
    for n, (mp, q) in enumerate(tasks, 1):
        stats.query += 1
        t0 = time.monotonic()
        try:
            found = ebay.search(q, mp, hunt.ebay)
        except EbayError as exc:
            stats.errori.append(f"{mp} '{q}': {exc}")
            log.error("[%s] ricerca %d/%d fallita %s '%s': %s", hunt.nome, n, len(tasks), mp, q, exc)
            if "budget" in str(exc):
                break
            continue
        new = sum(1 for l in found if l.legacy_id not in best)
        log.info("[%s] ricerca %d/%d %s '%s': %d risultati, %d mai visti in questo giro (%.1f s)",
                 hunt.nome, n, len(tasks), mp, q, len(found), new, time.monotonic() - t0)
        stats.trovati += len(found)
        per_mp[mp] += new
        for l in found:
            cur = best.get(l.legacy_id)
            if cur is None or l.total < cur.total:
                best[l.legacy_id] = l
    stats.unici = len(best)
    countries = Counter(l.country or "?" for l in best.values())
    log.info("[%s] %d annunci unici su %d trovati; contributo per marketplace: %s", hunt.nome,
             stats.unici, stats.trovati, dict(per_mp.most_common()))
    log.info("[%s] provenienza: %s", hunt.nome, dict(countries.most_common()))
    return list(best.values())


# ---------------------------------------------------------------------------
# 4. Scrematura (Palantir, titoli in blocco)
# ---------------------------------------------------------------------------
def screen(hunt: Hunt, plan: dict, items: list[Listing], palantir: LLMClient, db: DB,
           batch_size: int, stats: RunStats) -> None:
    n_batches = (len(items) + batch_size - 1) // batch_size
    log.info("[%s] scrematura di %d titoli in %d blocchi da %d", hunt.nome, len(items), n_batches, batch_size)
    for b, i in enumerate(range(0, len(items), batch_size), 1):
        batch = items[i:i + batch_size]
        t0 = time.monotonic()
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
        tally: Counter = Counter()
        for k, l in ids.items():
            verdict, reason = got.get(k, ("forse", "non valutato dal modello"))
            db.set_screen(hunt.nome, l.legacy_id, verdict, reason)
            tally[verdict] += 1
            log.debug("[%s]   %-5s %s | %s", hunt.nome, verdict, l.title[:80], reason)
            stats.scremati += 1
            if verdict == "no":
                stats.scremati_no += 1
        log.info("[%s] blocco %d/%d scremato in %.1f s: si %d, forse %d, no %d%s", hunt.nome, b, n_batches,
                 time.monotonic() - t0, tally["si"], tally["forse"], tally["no"],
                 f" ({len(ids) - len(got)} non valutati dal modello)" if len(got) < len(ids) else "")


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
    log.info("[%s] verifica Groq di %d annunci", hunt.nome, len(items))
    for n, l in enumerate(items, 1):
        t0 = time.monotonic()
        log.info("[%s] verifica %d/%d %s [%s %s] %.2f %s: %s", hunt.nome, n, len(items), l.legacy_id,
                 l.marketplace.removeprefix("EBAY_"), l.country or "?", l.total, l.currency, l.title[:70])
        # il confronto per la riverifica si fa sempre sul totale della ricerca, che e' quello
        # che rivedremo al prossimo giro (il dettaglio puo' riportare una spedizione diversa)
        summary_total = l.total
        try:
            ebay.get_item(l, cfg.description_max_chars)
        except EbayError as exc:
            log.warning("[%s] dettaglio non disponibile per %s: %s", hunt.nome, l.legacy_id, exc)
        try:
            res = groq.chat_json(prompts.VERIFY_SYSTEM,
                                 prompts.verify_user(hunt.ricerca, hunt.requisiti_avanzati, ref,
                                                     listing_payload(l)))
        except LLMError as exc:
            stats.errori.append(f"verifica {l.legacy_id}: {exc}")
            log.error("verifica fallita per %s: %s", l.legacy_id, exc)
            if "tentativi esauriti" in str(exc):
                log.warning("[%s] Groq non risponde: interrompo la verifica, %d annunci al prossimo giro",
                            hunt.nome, len(items) - n)
                break   # rate limit persistente: inutile insistere in questo giro
            continue
        if not isinstance(res, dict):
            log.warning("[%s] risposta di verifica non valida per %s, salto", hunt.nome, l.legacy_id)
            continue
        db.set_verify(hunt.nome, l.legacy_id, hunt.verify_hash, summary_total, res)
        stats.verificati += 1
        if res.get("esito") == "conforme":
            stats.conformi += 1
        log.info("[%s]   -> %s, punteggio %s (%.1f s)%s", hunt.nome, res.get("esito"), res.get("punteggio"),
                 time.monotonic() - t0, f": {str(res.get('sintesi'))[:120]}" if res.get("sintesi") else "")


# ---------------------------------------------------------------------------
# Orchestrazione
# ---------------------------------------------------------------------------
def _reason_key(reason: str) -> str:
    """Raggruppa i motivi di scarto per il riepilogo (senza cifre e nomi specifici)."""
    if reason.startswith("paese"):
        return "fuori area"
    if reason.startswith("totale"):
        return "oltre budget"
    if reason.startswith("parola esclusa"):
        return "parola esclusa"
    if reason.startswith("feedback"):
        return "feedback basso"
    if reason.startswith("valuta"):
        return "altra valuta"
    return reason


def run_hunt(hunt: Hunt, cfg: Config, db: DB, ebay: EbayClient, palantir: LLMClient | None,
             groq: LLMClient | None, force_plan: bool = False) -> RunStats:
    stats = RunStats()
    run_id = db.start_run(hunt.nome)
    calls_before = ebay.calls
    t_start = time.monotonic()
    error = None
    allowed = hunt.ebay.allowed_countries()
    log.info("[%s] === giro %d avviato: %d marketplace, paesi ammessi %s, budget eBay residuo %d chiamate ===",
             hunt.nome, run_id, len(hunt.ebay.marketplaces),
             "tutti" if allowed is None else ("UE27" if len(allowed) == 27 else ",".join(sorted(allowed))),
             ebay.call_budget - ebay.calls)
    try:
        with phase(stats, "piano", hunt.nome):
            plan = get_plan(hunt, db, pick_planner(cfg, palantir, groq), force=force_plan)
        excluded = [w.lower() for w in hunt.parole_escluse] + plan["parole_escluse"]

        with phase(stats, "ricerca", hunt.nome):
            listings = search_all(hunt, plan, ebay, stats)

        kept: list[Listing] = []
        reasons: Counter = Counter()
        for l in listings:
            reason = local_reject_reason(l, hunt, excluded)
            if reason:
                stats.scartati_filtri += 1
                if reason.startswith("paese"):
                    stats.scartati_paese += 1
                reasons[_reason_key(reason)] += 1
                log.debug("[%s] scartato %s (%s): %s", hunt.nome, l.legacy_id, l.title[:60], reason)
                continue
            if db.upsert_seen(hunt.nome, l):
                stats.nuovi += 1
            kept.append(l)
        log.info("[%s] filtri locali: tenuti %d, scartati %d %s; nuovi mai visti %d", hunt.nome, len(kept),
                 stats.scartati_filtri, dict(reasons.most_common()) if reasons else "", stats.nuovi)

        # scrematura solo per chi non e' mai stato scremato
        to_screen = [l for l in kept if (db.get_item(hunt.nome, l.legacy_id)["screen_verdict"] is None)]
        if to_screen:
            with phase(stats, "scrematura", hunt.nome):
                if hunt.screening and palantir is not None:
                    screen(hunt, plan, to_screen, palantir, db, cfg.screen_batch_size, stats)
                else:
                    log.info("[%s] scrematura disattivata: %d annunci passano come 'saltato'",
                             hunt.nome, len(to_screen))
                    for l in to_screen:
                        db.set_screen(hunt.nome, l.legacy_id, "saltato", "")
        else:
            log.info("[%s] scrematura: nessun annuncio nuovo da valutare", hunt.nome)

        # candidati alla verifica: si prima di forse, poi dal piu' economico
        cands = []
        already = 0
        for l in kept:
            row = db.get_item(hunt.nome, l.legacy_id)
            if row["screen_verdict"] == "no":
                continue
            if needs_verify(row, hunt, l.total):
                cands.append((SCREEN_ORDER.get(row["screen_verdict"], 2), l.total, l))
            else:
                already += 1
        cands.sort(key=lambda t: (t[0], t[1]))
        todo = [c[2] for c in cands[: cfg.max_verify_per_run]]
        log.info("[%s] verifica: %d candidati, %d gia' verificati e invariati", hunt.nome, len(cands), already)
        if len(cands) > len(todo):
            log.info("[%s] %d candidati rimandati al prossimo giro (limite %d)", hunt.nome,
                     len(cands) - len(todo), cfg.max_verify_per_run)

        if groq is not None and todo:
            with phase(stats, "verifica", hunt.nome):
                verify(hunt, todo, ebay, groq, db, cfg, stats)
        elif todo:
            log.warning("[%s] GROQ_API_KEY assente: verifica avanzata saltata per %d annunci", hunt.nome, len(todo))

        stats.notificati = notify(hunt, db, cfg)
        if stats.notificati:
            log.info("[%s] inviate %d notifiche ntfy", hunt.nome, stats.notificati)
    except Exception as exc:  # l'errore finisce nel DB e nel log, poi risale
        error = f"{type(exc).__name__}: {exc}"
        stats.errori.append(error)
        log.error("[%s] giro %d interrotto: %s", hunt.nome, run_id, error)
        raise
    finally:
        stats.chiamate_ebay = ebay.calls - calls_before
        if palantir:
            stats.token_palantir = palantir.tokens_in + palantir.tokens_out
        if groq:
            stats.token_groq = groq.tokens_in + groq.tokens_out
        stats.durate["totale"] = round(time.monotonic() - t_start, 1)
        db.finish_run(run_id, stats.__dict__, error)
        log.info("[%s] === giro %d concluso in %.0f s: %d chiamate eBay, token Palantir %d, token Groq %d, "
                 "verificati %d (conformi %d), errori %d ===", hunt.nome, run_id, stats.durate["totale"],
                 stats.chiamate_ebay, stats.token_palantir, stats.token_groq, stats.verificati,
                 stats.conformi, len(stats.errori))
    return stats
