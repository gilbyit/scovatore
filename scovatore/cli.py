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
from .hunt import Hunt, HuntError, load_all, load_hunt
from .llm import LLMClient, LLMError
from .pipeline import get_plan, pick_planner, run_hunt

log = logging.getLogger("scovatore")


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
    plan = get_plan(hunt, db, pick_planner(cfg, palantir, groq), force=args.rigenera)
    print(f"Caccia: {hunt.nome}")
    print(f"Piano generato da: {plan.get('generato_da', '?')} (SCOVATORE_PLAN_LLM={cfg.plan_llm})")
    print(f"Filtro eBay: {build_filter(hunt.ebay)}")
    print(f"Marketplace: {', '.join(hunt.ebay.marketplaces)}")
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
    if s.non_spediscono:
        print(f"scartati in verifica perche' non spediscono qui {s.non_spediscono}")
    if s.scartati_paese or s.ricerche_saltate:
        print(f"scartati per paese {s.scartati_paese} | ricerche saltate per max_ricerche {s.ricerche_saltate}")
    if s.durate:
        print("durate: " + ", ".join(f"{k} {v:.0f}s" for k, v in s.durate.items()))
    for e in s.errori:
        print(f"  ! {e}")


def _run(cfg: Config, hunt: Hunt, force_plan: bool = False):
    db = DB(cfg.db_path)
    try:
        ebay, palantir, groq = _clients(cfg, db=db)
    except EbayError:
        db.close()
        raise
    try:
        stats = run_hunt(hunt, cfg, db, ebay, palantir, groq, force_plan=force_plan)
    finally:
        for c in (ebay, palantir, groq):
            if c:
                c.close()
        db.close()
    _print_stats(hunt.nome, stats)
    return stats


def cmd_esegui(cfg: Config, args) -> int:
    hunt = _resolve(cfg, args.caccia)
    _run(cfg, hunt, force_plan=args.rigenera_piano)
    _show(cfg, hunt.nome, min_score=0, limit=args.mostra, tutti=False)
    return 0


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


def cmd_loop(cfg: Config, args) -> int:
    log.info("loop avviato, controllo ogni %d minuti", args.intervallo)
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
        time.sleep(args.intervallo * 60)


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
        print(f"{'':>29}{cfg.item_link(r['legacy_id'])}")
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["punteggio", "esito", "totale", "valuta", "titolo", "url", "sintesi", "rischi"])
            for r in rows:
                v = json.loads(r["verify_json"] or "{}")
                w.writerow([r["score"], r["verdict"], r["total"], r["currency"], r["title"], cfg.item_link(r["legacy_id"]),
                            v.get("sintesi", ""), "; ".join(v.get("segnali_rischio") or [])])
        print(f"\nCSV scritto in {csv_path}")


def cmd_risultati(cfg: Config, args) -> int:
    hunt = _resolve(cfg, args.caccia)
    _show(cfg, hunt.nome, args.min, args.limite, args.tutti, args.csv)
    return 0


def cmd_controlla(cfg: Config, args) -> int:
    ok = True
    for name, val in (("EBAY_APP_ID", cfg.ebay_app_id), ("EBAY_CERT_ID", cfg.ebay_cert_id),
                      ("PALANTIR_BASE_URL", cfg.palantir.base_url), ("GROQ_API_KEY", cfg.groq.api_key)):
        print(f"{'ok ' if val else 'MANCA'} {name}")
        ok &= bool(val) or name == "GROQ_API_KEY"
    print(f"ok  piano generato da: {cfg.plan_llm}")
    if cfg.plan_llm == "groq" and not cfg.groq.api_key:
        print("ATTENZIONE SCOVATORE_PLAN_LLM=groq ma GROQ_API_KEY manca: il piano lo fara' Palantir")
    try:
        hunts = load_all(cfg.hunts_dir)
        for h in hunts:
            h.reference_text()
            print(f"ok  caccia {h.nome} ({'attiva' if h.attiva else 'disattiva'}, ogni {h.ogni_minuti} min) "
                  f"filtro: {build_filter(h.ebay)}")
            allowed = h.ebay.allowed_countries()
            print(f"      marketplace: {','.join(m.removeprefix('EBAY_') for m in h.ebay.marketplaces)} | "
                  f"paesi ammessi: {'tutti' if allowed is None else 'UE27' if len(allowed) == 27 else ','.join(sorted(allowed))}"
                  f" | max_ricerche {h.ebay.max_ricerche} | lingue {','.join(h.languages())}")
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
