"""Riga di comando: python -m scovatore <comando>."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, load_config
from .db import DB
from .ebay import EbayClient, EbayError, build_filter
from .hunt import SOURCE_LABELS, SOURCES, Hunt, HuntError, load_all, load_hunt
from .llm import LLMClient, LLMError
from .pipeline import get_plan, pick_planner, plan_key, run_hunt

log = logging.getLogger("scovatore")

REQUEST_POLL_SECONDS = 10   # ogni quanto il loop guarda le richieste di riesecuzione dell'interfaccia web


def _clients(cfg: Config, need_ebay: bool = True, db: DB | None = None):
    ebay = None
    if need_ebay:
        used = db.ebay_calls_today() if db else 0
        remaining = max(0, cfg.ebay_daily_call_budget - used)
        if remaining == 0:
            raise EbayError(f"budget giornaliero eBay esaurito ({used} chiamate oggi)")
        ebay = EbayClient(cfg.ebay_app_id, cfg.ebay_cert_id, cfg.ebay_api_base, cfg.ebay_buyer_country,
                          cfg.ebay_buyer_zip, remaining)
    palantir = LLMClient(cfg.palantir) if cfg.palantir.base_url else None
    groq = LLMClient(cfg.groq) if cfg.groq.api_key else None
    return ebay, palantir, groq


def _resolve(cfg: Config, name_or_path: str) -> Hunt:
    p = Path(name_or_path)
    if not p.exists():
        for ext in (".yaml", ".yml"):
            cand = cfg.hunts_dir / f"{name_or_path}{ext}"
            if cand.exists():
                p = cand
                break
    return load_hunt(p)


def cmd_piano(cfg: Config, args) -> int:
    hunt = _resolve(cfg, args.caccia)
    db = DB(cfg.db_path)
    _, palantir, groq = _clients(cfg, need_ebay=False)
    planner = pick_planner(cfg, palantir, groq)
    print(f"Caccia: {hunt.nome} (fonti: {', '.join(hunt.fonti)})")
    done: set[str] = set()
    for s in hunt.fonti:
        key = plan_key(hunt, planner, s)
        if key in done:        # stessa nota sui filtri: stesso piano, gia' mostrato
            print(f"\nPiano {SOURCE_LABELS[s]}: identico a quello gia' mostrato")
            continue
        plan = get_plan(hunt, db, planner, force=args.rigenera and key not in done, source=s)
        done.add(key)
        print(f"\nPiano per {SOURCE_LABELS[s]}, generato da: {plan.get('generato_da', '?')} "
              f"(SCOVATORE_PLAN_LLM={cfg.plan_llm})")
        if s == "ebay":
            print(f"Filtro eBay: {build_filter(hunt.ebay)}")
            print(f"Marketplace: {', '.join(hunt.ebay.marketplaces)}")
        elif s == "vinted":
            print(f"Domini Vinted: {', '.join(hunt.vinted.domini)} (query solo nella lingua del dominio)")
        else:
            print(f"Subito: regione {hunt.subito.regione}, categoria {hunt.subito.categoria} (query in italiano)")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    if hunt.query_extra:
        print(f"Query extra: {hunt.query_extra}")
    return 0


def _print_stats(name: str, s) -> None:
    print(f"\n== {name} ==")
    print(f"query {s.query} | trovati {s.trovati} | unici {s.unici} | scartati dai filtri {s.scartati_filtri} "
          f"| nuovi {s.nuovi}")
    print(f"scremati {s.scremati} (no: {s.scremati_no}) | verificati {s.verificati} | conformi {s.conformi} "
          f"| notificati {s.notificati}")
    print(f"chiamate eBay {s.chiamate_ebay} | token Palantir {s.token_palantir} | token Groq {s.token_groq}")
    if s.richieste:
        print("richieste fonti web: " + ", ".join(
            f"{SOURCE_LABELS.get(k, k)} {n} (trovati {s.trovati_fonte.get(k, 0)})" for k, n in s.richieste.items()))
    if s.non_spediscono:
        print(f"scartati in verifica perche' non spediscono qui {s.non_spediscono}")
    if s.scartati_paese or s.ricerche_saltate:
        print(f"scartati per paese {s.scartati_paese} | ricerche saltate per max_ricerche {s.ricerche_saltate}")
    if s.durate:
        print("durate: " + ", ".join(f"{k} {v:.0f}s" for k, v in s.durate.items()))
    for e in s.errori:
        print(f"  ! {e}")


def _parse_sources(raw: str | None, hunt: Hunt) -> list[str] | None:
    """--fonte ebay,vinted -> ["ebay", "vinted"]; None o "tutte" = tutte le fonti della caccia."""
    if not raw or raw.strip().lower() in ("tutte", "tutti", "*"):
        return None
    out = [s.strip().lower() for s in raw.split(",") if s.strip()]
    bad = [s for s in out if s not in SOURCES]
    if bad:
        raise HuntError(f"fonte sconosciuta: {bad} (valide: {', '.join(SOURCES)})")
    off = [s for s in out if s not in hunt.fonti]
    if off:
        raise HuntError(f"la caccia {hunt.nome} non ha attiva la fonte {off} (fonti: {', '.join(hunt.fonti)}): "
                        f"aggiungila al campo `fonti` del file YAML")
    return out


def _run(cfg: Config, hunt: Hunt, force_plan: bool = False, only: list[str] | None = None):
    db = DB(cfg.db_path)
    active = [s for s in hunt.fonti if only is None or s in only]
    try:
        # il client eBay serve (e il suo budget conta) solo se eBay e' tra le fonti di questo giro
        ebay, palantir, groq = _clients(cfg, need_ebay="ebay" in active, db=db)
    except EbayError:
        db.close()
        raise
    try:
        stats = run_hunt(hunt, cfg, db, ebay, palantir, groq, force_plan=force_plan, only=only)
    finally:
        for c in (ebay, palantir, groq):
            if c:
                c.close()
        db.close()
    _print_stats(hunt.nome, stats)
    return stats


def cmd_esegui(cfg: Config, args) -> int:
    hunt = _resolve(cfg, args.caccia)
    _run(cfg, hunt, force_plan=args.rigenera_piano, only=_parse_sources(args.fonte, hunt))
    _show(cfg, hunt.nome, min_score=0, limit=args.mostra, tutti=False)
    return 0


def process_requests(cfg: Config) -> int:
    """Esegue le riesecuzioni chieste dall'interfaccia web (pulsante "Riesegui ora").

    Il web e il loop sono container diversi: il web scrive la richiesta nel DB, il loop la
    prende qui. Ignora ogni_minuti e anche `attiva: false`, perche' l'operatore l'ha chiesta.
    """
    db = DB(cfg.db_path)
    try:
        pending = db.pending_requests()
    finally:
        db.close()
    done = 0
    for req in pending:
        db = DB(cfg.db_path)
        try:
            if not db.claim_request(req["id"]):
                continue            # un altro processo l'ha gia' presa
        finally:
            db.close()
        error = None
        try:
            hunt = next((h for h in load_all(cfg.hunts_dir) if h.nome == req["hunt"]), None)
            if hunt is None:
                raise HuntError(f"caccia {req['hunt']!r} non trovata in {cfg.hunts_dir}")
            only = _parse_sources(req["fonti"], hunt)
            log.info("riesecuzione richiesta dall'interfaccia: %s (fonti %s%s)", hunt.nome,
                     ",".join(only) if only else "tutte", ", piano rigenerato" if req["rigenera_piano"] else "")
            _run(cfg, hunt, force_plan=bool(req["rigenera_piano"]), only=only)
            done += 1
        except Exception as exc:    # l'errore resta visibile nell'interfaccia
            error = f"{type(exc).__name__}: {exc}"
            log.error("riesecuzione di %s fallita: %s", req["hunt"], error)
        finally:
            db = DB(cfg.db_path)
            try:
                db.finish_request(req["id"], error)
            finally:
                db.close()
    return done


def _due(cfg: Config, hunt: Hunt) -> bool:
    db = DB(cfg.db_path)
    try:
        last = db.last_run_at(hunt.nome)
    finally:
        db.close()
    if not last:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(minutes=hunt.ogni_minuti)


def _minutes_to_due(cfg: Config, hunt: Hunt) -> float:
    db = DB(cfg.db_path)
    try:
        last = db.last_run_at(hunt.nome)
    finally:
        db.close()
    if not last:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
    return max(0.0, hunt.ogni_minuti - elapsed)


def cmd_tutte(cfg: Config, args) -> int:
    rc = 0
    hunts = load_all(cfg.hunts_dir)
    log.info("controllo %d cacce (%d attive)", len(hunts), sum(h.attiva for h in hunts))
    for hunt in hunts:
        if not hunt.attiva:
            log.info("%s: disattivata, salto", hunt.nome)
            continue
        if not args.forza and not _due(cfg, hunt):
            log.info("%s: non ancora dovuta, prossimo giro fra %.0f min (ogni %d)", hunt.nome,
                     _minutes_to_due(cfg, hunt), hunt.ogni_minuti)
            continue
        try:
            _run(cfg, hunt)
        except Exception as exc:
            log.exception("caccia %s fallita: %s", hunt.nome, exc)
            rc = 1
    return rc


def _wait_for_requests(cfg: Config, seconds: float) -> None:
    """Attende `seconds` secondi, ma ogni pochi secondi guarda se l'interfaccia web ha chiesto
    una riesecuzione e, in caso, la esegue subito."""
    end = time.monotonic() + seconds
    while True:
        try:
            process_requests(cfg)
        except Exception as exc:  # il loop non deve morire per un errore imprevisto
            log.exception("errore nell'elaborare le richieste di riesecuzione: %s", exc)
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(REQUEST_POLL_SECONDS, left))


def cmd_loop(cfg: Config, args) -> int:
    log.info("loop avviato, controllo ogni %d minuti (richieste dall'interfaccia web ogni %d s)",
             args.intervallo, REQUEST_POLL_SECONDS)
    while True:
        try:
            args.forza = False
            cmd_tutte(cfg, args)
        except HuntError as exc:
            log.error("definizione cacce non valida: %s", exc)
        except Exception as exc:  # il loop non deve morire per un errore imprevisto
            log.exception("errore nel giro di controllo: %s", exc)
        nxt = datetime.now() + timedelta(minutes=args.intervallo)
        log.info("in attesa, prossimo controllo alle %s", nxt.strftime("%H:%M"))
        _wait_for_requests(cfg, args.intervallo * 60)


def _show(cfg: Config, name: str, min_score: int, limit: int, tutti: bool, csv_path: str | None = None):
    db = DB(cfg.db_path)
    rows = db.results(name, min_score=min_score, limit=limit, include_unverified=tutti)
    db.close()
    if not rows:
        print("Nessun risultato verificato.")
        return
    print(f"\n{'punti':>5} {'esito':<13} {'totale':>8} {'paese':<5} titolo")
    for r in rows:
        score = "-" if r["score"] is None else str(r["score"])
        verdict = r["verdict"] or f"({r['screen_verdict'] or '?'})"
        print(f"{score:>5} {verdict:<13} {r['total']:>8.2f} {r['country'] or '?':<5} {r['title'][:66]}")
        if r["verify_json"]:
            v = json.loads(r["verify_json"])
            if v.get("sintesi"):
                print(f"{'':>29}{v['sintesi'][:150]}")
        print(f"{'':>29}{cfg.item_link(r['legacy_id'], r['url'] or '')}")
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["punteggio", "esito", "totale", "valuta", "titolo", "url", "sintesi", "rischi"])
            for r in rows:
                v = json.loads(r["verify_json"] or "{}")
                w.writerow([r["score"], r["verdict"], r["total"], r["currency"], r["title"],
                            cfg.item_link(r["legacy_id"], r["url"] or ""),
                            v.get("sintesi", ""), "; ".join(v.get("segnali_rischio") or [])])
        print(f"\nCSV scritto in {csv_path}")


def cmd_risultati(cfg: Config, args) -> int:
    hunt = _resolve(cfg, args.caccia)
    _show(cfg, hunt.nome, args.min, args.limite, args.tutti, args.csv)
    return 0


def cmd_controlla(cfg: Config, args) -> int:
    ok = True
    try:
        hunts = load_all(cfg.hunts_dir)
    except HuntError as exc:
        print(f"ERRORE {exc}")
        hunts, ok = [], False
    uses_ebay = any(h.uses("ebay") for h in hunts) or not hunts   # senza cacce: si controlla comunque
    for name, val in (("EBAY_APP_ID", cfg.ebay_app_id), ("EBAY_CERT_ID", cfg.ebay_cert_id),
                      ("PALANTIR_BASE_URL", cfg.palantir.base_url), ("GROQ_API_KEY", cfg.groq.api_key)):
        print(f"{'ok ' if val else 'MANCA'} {name}")
        optional = name == "GROQ_API_KEY" or (name.startswith("EBAY_") and not uses_ebay)
        ok &= bool(val) or optional
    print(f"ok  piano generato da: {cfg.plan_llm}")
    if cfg.plan_llm == "groq" and not cfg.groq.api_key:
        print("ATTENZIONE SCOVATORE_PLAN_LLM=groq ma GROQ_API_KEY manca: il piano lo fara' Palantir")
    try:
        for h in hunts:
            h.reference_text()
            print(f"ok  caccia {h.nome} ({'attiva' if h.attiva else 'disattiva'}, ogni {h.ogni_minuti} min) "
                  f"fonti: {', '.join(h.fonti)}")
            if h.uses("ebay"):
                allowed = h.ebay.allowed_countries()
                print(f"      eBay filtro: {build_filter(h.ebay)}")
                print(f"      marketplace: {','.join(m.removeprefix('EBAY_') for m in h.ebay.marketplaces)} | "
                      f"paesi ammessi: {'tutti' if allowed is None else 'UE27' if len(allowed) == 27 else ','.join(sorted(allowed))}"
                      f" | max_ricerche {h.ebay.max_ricerche}")
            if h.uses("vinted"):
                print(f"      Vinted: domini {','.join(h.vinted.domini)} | prezzo {h.price_limits('vinted')} "
                      f"| max_ricerche {h.vinted.max_ricerche}")
            if h.uses("subito"):
                print(f"      Subito: regione {h.subito.regione}, categoria {h.subito.categoria} | prezzo "
                      f"{h.price_limits('subito')} | max_ricerche {h.subito.max_ricerche}")
            print(f"      lingue del piano: {','.join(h.languages())}")
    except HuntError as exc:
        print(f"ERRORE {exc}")
        ok = False
    return 0 if ok else 1


def cmd_web(cfg: Config, args) -> int:
    from .web import serve
    serve(cfg, args.host, args.porta)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scovatore", description="Cacciatore di annunci eBay con LLM")
    ap.add_argument("--env", help="file .env alternativo")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("piano", help="mostra le query generate da Palantir, senza chiamare eBay")
    p.add_argument("caccia")
    p.add_argument("--rigenera", action="store_true", help="ignora il piano in cache")
    p.set_defaults(fn=cmd_piano)

    p = sub.add_parser("esegui", help="esegue una caccia")
    p.add_argument("caccia")
    p.add_argument("--rigenera-piano", action="store_true")
    p.add_argument("--fonte", help="solo queste fonti della caccia, per esempio ebay,vinted (default: tutte)")
    p.add_argument("--mostra", type=int, default=15, help="quanti risultati stampare alla fine")
    p.set_defaults(fn=cmd_esegui)

    p = sub.add_parser("tutte", help="esegue le cacce attive e scadute")
    p.add_argument("--forza", action="store_true", help="ignora ogni_minuti")
    p.set_defaults(fn=cmd_tutte)

    p = sub.add_parser("loop", help="modalita' servizio: esegue le cacce quando scadono")
    p.add_argument("--intervallo", type=int, default=10, help="minuti fra un controllo e l'altro")
    p.set_defaults(fn=cmd_loop)

    p = sub.add_parser("risultati", help="classifica degli annunci verificati")
    p.add_argument("caccia")
    p.add_argument("--min", type=int, default=0, help="punteggio minimo")
    p.add_argument("--limite", type=int, default=30)
    p.add_argument("--tutti", action="store_true", help="includi anche i non verificati")
    p.add_argument("--csv", help="scrivi anche un CSV")
    p.set_defaults(fn=cmd_risultati)

    p = sub.add_parser("web", help="interfaccia web sui risultati, con interventi manuali")
    p.add_argument("--host", help="default SCOVATORE_WEB_HOST (0.0.0.0)")
    p.add_argument("--porta", type=int, help="default SCOVATORE_WEB_PORT (8482)")
    p.set_defaults(fn=cmd_web)

    p = sub.add_parser("controlla", help="verifica .env e definizioni delle cacce")
    p.set_defaults(fn=cmd_controlla)

    args = ap.parse_args(argv)
    cfg = load_config(args.env)
    logging.basicConfig(level="DEBUG" if args.verbose else cfg.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return args.fn(cfg, args)
    except HuntError as exc:
        print(f"Errore nella caccia: {exc}", file=sys.stderr)
        return 2
    except (EbayError, LLMError) as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
