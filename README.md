# Scovatore

Cacciatore di annunci eBay per NASGUL. Gli descrivi a parole cosa cerchi, senza marca e modello, e lui:

1. fa generare a **Palantir** le query di ricerca (sinonimi, varianti, più lingue);
2. lancia le ricerche sulla **Browse API** di eBay con i filtri strutturati (prezzo, area geografica, condizione...);
3. scarta in locale quello che non rientra (budget con spedizione, valuta, parole escluse, feedback);
4. fa scremare i titoli a **Palantir**, a blocchi (si / forse / no);
5. manda i sopravvissuti, con descrizione e specifiche complete, a **Groq** per la verifica dei requisiti avanzati;
6. salva tutto in SQLite, stila una classifica e, se configurato, notifica via **ntfy** i nuovi annunci sopra soglia;
7. mostra i risultati in una piccola **interfaccia web** (servizio `scovatore-web`, porta 8482).

Niente scraping HTML: usa l'API ufficiale, gratuita con un account developer.

```
 cacce/*.yaml
      |
      v
 [Palantir] piano ---> query per lingua (in cache finche' la ricerca non cambia)
      |
      v
 [eBay Browse API] ricerca x marketplace x query  (filtri eBay = parametri YAML)
      |
      v
 filtri locali: totale con spedizione, valuta, parole escluse, feedback
      |
      v
 [Palantir] scrematura titoli a blocchi ---> "no" scartati
      |
      v
 [eBay] dettaglio annuncio  +  [Groq] verifica requisiti avanzati ---> punteggio 0-100
      |
      v
 SQLite  ->  classifica / CSV / notifica ntfy
```

## Divisione dei compiti

| Cosa | Chi | Perché |
|---|---|---|
| Prezzo, valuta, area geografica, paese di consegna, condizione, formato, venditori | eBay (parametri YAML) | eBay li filtra meglio e gratis |
| Budget con spedizione inclusa, parole escluse, feedback minimo | Scovatore in locale | deterministico, zero token |
| Query sinonime a partire dagli elementi chiave | Palantir | è lavoro linguistico, gira in locale, nessun limite di token |
| Scrematura dei titoli | Palantir | taglia il rumore prima di spendere token Groq |
| Requisiti avanzati (TDP, PassMark, diagnosi da sintomi...) | Groq | serve conoscenza di dominio, un modello da 4B non basta |

## Installazione

```bash
git clone <repo> scovatore && cd scovatore
cp .env.example .env
# compila EBAY_APP_ID, EBAY_CERT_ID, PALANTIR_BASE_URL, PALANTIR_API_KEY, GROQ_API_KEY
```

### Chiavi eBay

1. Registrati su https://developer.ebay.com (account gratuito).
2. *Application Keys* > crea un keyset **Production**.
3. Copia **App ID (Client ID)** in `EBAY_APP_ID` e **Cert ID (Client Secret)** in `EBAY_CERT_ID`.

Serve solo il token applicativo (client credentials): nessun login utente, nessun redirect OAuth. Il limite di default della Browse API è 5000 chiamate al giorno; Scovatore le conta nel DB e si ferma a `EBAY_DAILY_CALL_BUDGET`.

### Palantir

`PALANTIR_BASE_URL` è l'endpoint OpenAI-compatible del gateway, con `/v1` finale. Crea in Palantir una chiave dedicata a Scovatore e mettila in `PALANTIR_API_KEY`.

- **Container**: il `docker-compose.yml` aggancia Scovatore alla rete Docker di Palantir (`palantir_default`, controlla il nome reale con `docker network ls`) e usa il nome del servizio del gateway come host. Il valore di default nel `.env.example` (`palantir-gateway:8080`) è un segnaposto: sostituiscilo con nome e porta reali.
- **Riga di comando sull'host**: usa `http://localhost:<porta>/v1`.

Se il gateway non accetta `response_format`, Scovatore se ne accorge al primo 400 e prosegue senza. Il JSON viene estratto anche da risposte sporche (blocchi `<think>`, recinti markdown, testo intorno).

### Groq

Metti la chiave in `GROQ_API_KEY`. Default: `openai/gpt-oss-120b` con `reasoning_effort=low`. Il limitatore interno rispetta `GROQ_TPM_LIMIT` (8000 token al minuto sul piano gratuito) e in caso di 429 aspetta quanto indicato da `retry-after`.

Senza chiave Groq la pipeline si ferma alla scrematura: gli annunci restano visibili con `risultati --tutti`.

## Uso

### Docker (servizio su NASGUL)

```bash
docker compose up -d --build
docker logs -f scovatore
```

Il container gira in `loop`: ogni 10 minuti controlla quali cacce sono scadute (campo `ogni_minuti`) e le esegue. Cacce e dati di riferimento sono montati in sola lettura: per aggiungere o modificare una caccia basta toccare il file YAML, senza riavviare.

Comandi a mano dentro il container:

```bash
docker exec -it scovatore python -m scovatore controlla
docker exec -it scovatore python -m scovatore piano mobo-ddr3-quad
docker exec -it scovatore python -m scovatore esegui mobo-ddr3-quad
docker exec -it scovatore python -m scovatore risultati mobo-ddr3-quad --min 60
```

### Riga di comando

```bash
pip install -r requirements.txt
python -m scovatore controlla                         # .env e cacce valide?
python -m scovatore piano ampli-guasto                # solo query di Palantir, nessuna chiamata eBay
python -m scovatore piano ampli-guasto --rigenera     # ignora il piano in cache
python -m scovatore esegui ampli-guasto               # giro completo + classifica
python -m scovatore tutte [--forza]                   # tutte le cacce attive scadute
python -m scovatore loop --intervallo 10              # modalità servizio
python -m scovatore risultati ampli-guasto --min 60 --csv ampli.csv
python -m scovatore risultati ampli-guasto --tutti    # anche i non verificati
```

`-v` attiva il log di debug (mostra anche il motivo di ogni scarto locale).

### Interfaccia web

Il compose avvia anche `scovatore-web`, che legge lo stesso database in sola lettura:

- `http://nasgul:8482/`: riepilogo delle cacce (conformi, verificati, in attesa, ultimo giro ed eventuali errori);
- `/caccia/<nome>`: classifica filtrabile per esito, punteggio minimo, paese, periodo, con ordinamento per punteggio, prezzo o novità. Ogni annuncio ha il dettaglio della verifica (requisiti, fonte del dato, rischi, domande al venditore) e il motivo della scrematura;
- `/giri`: gli ultimi giri di tutte le cacce con durata di ogni fase, contatori ed errori;
- `/api/caccia/<nome>` e `/api/giri`: gli stessi dati in JSON (accettano gli stessi parametri della pagina), utili per GILPA.

Senza `SCOVATORE_WEB_TOKEN` non c'è protezione: va bene finché la porta resta in LAN. Con il token impostato si apre una volta `http://nasgul:8482/?token=...` e il browser lo ricorda. Da riga di comando: `python -m scovatore web [--porta 8482]`.

### Log

A livello `INFO` (default) il log dice sempre cosa sta facendo: inizio e fine di ogni fase con la durata, ogni ricerca (marketplace, query, risultati, quanti mai visti nel giro), la provenienza degli annunci per paese, gli scarti dei filtri locali raggruppati per motivo, ogni blocco di scrematura con il conteggio si/forse/no, ogni verifica con esito e punteggio, e un riepilogo a fine giro. Nel `loop` dice anche quando scade ogni caccia e a che ora c'è il prossimo controllo. Le chiamate LLM oltre i 60 secondi vengono segnalate.

Con `-v` o `SCOVATORE_LOG_LEVEL=DEBUG` si aggiungono il motivo di ogni singolo scarto, le query del piano, i parametri di ogni chiamata eBay e tempi e token di ogni chiamata LLM.

**Consiglio per la prima volta**: lancia `piano` e guarda le query prima di `esegui`. Se Palantir produce query troppo generiche o troppo specifiche, correggi `ricerca` oppure aggiungi `query_extra`.

## Definire una caccia

Un file YAML in `cacce/`. Esempi completi in `cacce/mobo-ddr3-quad.yaml` e `cacce/ampli-guasto.yaml`.

```yaml
nome: ampli-guasto
attiva: true
ogni_minuti: 240

ebay:                          # tutto quello che eBay sa filtrare
  marketplaces: ue             # tutti i siti eBay della UE
  paesi_ammessi: ue            # oggetto situato in uno dei 27 paesi UE
  prezzo_max: 150
  spedizione_inclusa: true
  regione: EUROPEAN_UNION
  consegna_paese: IT
  condizioni: [guasto]

ricerca: >                     # per Palantir: cosa cercare
  Amplificatore audio hi-fi guasto o non funzionante, venduto per ricambi.

requisiti_avanzati: |          # per Groq: cosa verificare sul dettaglio
  - Marca di buona qualità costruttiva.
  - Sintomi compatibili con un guasto di alimentazione, non dei finali.
```

### Sezione `ebay`

| Campo | Default | Filtro eBay / effetto |
|---|---|---|
| `marketplaces` | `ue` | un giro di query per ciascuno, nella sua lingua più l'inglese. `ue` = `EBAY_IT, DE, FR, ES, NL, BE, AT, IE, PL`; si può mescolare: `[ue, EBAY_GB]` |
| `paesi_ammessi` | `ue` | controllo locale sul paese in cui si trova l'oggetto: `ue` (i 27 stati membri), una lista ISO (`[IT, DE, LT]`, anche `[ue, CH]`) oppure `tutti` |
| `max_ricerche` | `150` | tetto di ricerche (query x marketplace) per giro; se taglia, taglia le query meno importanti su tutti i marketplace |
| `valuta` | `EUR` | `priceCurrency`; gli annunci in altra valuta vengono scartati |
| `prezzo_min`, `prezzo_max` | nessuno | `price:[min..max]` |
| `spedizione_inclusa` | `true` | il budget vale su prezzo + spedizione (controllo locale) |
| `spedizione_ignota` | `tieni` | `tieni` o `scarta` gli annunci senza costo di spedizione noto |
| `regione` | `EUROPEAN_UNION` | `itemLocationRegion` (anche `CONTINENTAL_EUROPE`, `WORLDWIDE`...) |
| `paese` | nessuno | `itemLocationCountry`, alternativo a `regione` (eBay rifiuta entrambi) |
| `consegna_paese` | `IT` | `deliveryCountry`: solo annunci che spediscono lì |
| `condizioni` | tutte | `conditionIds`: numeri o alias `nuovo`, `aperto`, `ricondizionato`, `usato`, `guasto` (= 7000, "per ricambi o non funzionante") |
| `formati` | tutti | `buyingOptions`: `FIXED_PRICE`, `AUCTION`, `BEST_OFFER` |
| `solo_spedizione_gratuita` | `false` | `maxDeliveryCost:0` |
| `tipo_venditore` | nessuno | `sellerAccountTypes`: `BUSINESS` o `INDIVIDUAL` |
| `escludi_venditori` | `[]` | `excludeSellers` |
| `cerca_in_descrizione` | `false` | `searchInDescription:true` (più risultati, più rumore) |
| `categorie` | nessuna | `category_ids` per marketplace: `{EBAY_IT: [1244], EBAY_DE: [1244]}` |
| `ordinamento` | `newlyListed` | `sort`: `price`, `-price`, `newlyListed`, `endingSoonest` |
| `max_risultati_per_query` | `100` | paginazione, massimo 200 per pagina |
| `feedback_minimo` | nessuno | % minima di feedback del venditore (controllo locale) |

#### Tutta la UE, non solo i paesi con un eBay

Non esiste un eBay per ogni paese: chi vende dalla Lituania, dalla Repubblica Ceca o dalla Slovenia pubblica su uno dei siti esistenti, quasi sempre ebay.de. Per questo la copertura UE sta in due filtri, non nell'elenco dei marketplace:

- `regione: EUROPEAN_UNION` chiede a eBay solo oggetti situati nella UE (supportato da tutti i marketplace europei);
- `paesi_ammessi: ue` ricontrolla in locale il paese dell'annuncio contro i 27 stati membri. Serve perché le aree geografiche di eBay non coincidono per forza con l'unione doganale: Regno Unito, Svizzera e Norvegia costano dogana e IVA all'import.

I marketplace in più servono a far girare le query nella lingua locale (un venditore polacco scrive il titolo in polacco anche su ebay.de), e come rete di sicurezza per gli annunci visibili solo su un sito. Il log di ogni giro dice quanti annunci nuovi ha portato ciascun marketplace: se uno porta sempre zero, toglilo dalla lista.

**Costo in chiamate.** Con 9 marketplace, 4 query per lingua e 2 `query_extra` un giro fa circa 85 ricerche più fino a 25 dettagli. Una caccia ogni 3 ore sta intorno alle 900 chiamate al giorno, contro le 5000 di eBay. Cambiare i marketplace cambia le lingue, quindi al primo giro Palantir rigenera il piano.

Nota: secondo la documentazione eBay, `buyingOptions` funziona in modo affidabile solo insieme a una categoria foglia. Senza `categorie` è meglio lasciarlo vuoto: altrimenti si rischiano risultati mancanti.

### Altri campi

| Campo | Default | Uso |
|---|---|---|
| `ricerca` | obbligatorio | testo libero per Palantir: cosa cercare. Solo gli elementi che finiscono nei titoli degli annunci |
| `requisiti_avanzati` | vuoto | testo libero per Groq: i criteri che richiedono conoscenza o lettura della descrizione |
| `query_extra` | `[]` | query scritte a mano, usate su tutti i marketplace insieme a quelle generate |
| `parole_escluse` | `[]` | scarto locale sul titolo, a parola intera. Si sommano a quelle proposte da Palantir |
| `screening` | `true` | scrematura dei titoli con Palantir. Con `false` va tutto a Groq (attenzione ai token) |
| `dati_riferimento` | nessuno | file di testo o CSV, percorso relativo al file YAML, passato a Groq come fonte da preferire alla sua memoria |
| `max_query_per_lingua` | `4` | tetto sulle query generate per ciascuna lingua |
| `attiva` | `true` | le cacce disattivate non girano in `loop` e `tutte` |
| `ogni_minuti` | `180` | intervallo minimo fra due esecuzioni automatiche |

## Cosa rigira e cosa no

Ogni giro ricerca sempre su eBay, ma i passaggi LLM si ripetono solo quando serve:

- **piano**: in cache finché non cambiano `ricerca`, marketplace (lingue) o `max_query_per_lingua`. Forzabile con `--rigenera` / `--rigenera-piano`;
- **scrematura**: una volta per annuncio;
- **verifica Groq**: una volta per annuncio, ripetuta se cambiano `ricerca`, `requisiti_avanzati` o `dati_riferimento`, oppure se il prezzo totale si muove di oltre il 5% (aste, ribassi);
- **notifica**: una volta per annuncio.

Per ogni giro la verifica tocca al massimo `SCOVATORE_MAX_VERIFY_PER_RUN` annunci, prima i "si" poi i "forse", dal più economico. Gli altri restano per il giro successivo. Con 8000 token al minuto e circa 1500-2500 token per verifica, 25 annunci richiedono qualche minuto di attesa.

## Uscita della verifica

Groq restituisce per ogni annuncio:

```json
{
  "oggetto": "Asus P9X79 + Xeon E5-xxxx v2, RAM non inclusa",
  "esito": "conforme | non_conforme | incerto",
  "punteggio": 78,
  "requisiti": [
    {"requisito": "TDP < 100 W", "stato": "ok", "valore": "...", "fonte": "riferimento", "nota": "..."}
  ],
  "segnali_rischio": ["foto stock", "CPU non visibile in foto"],
  "domande_al_venditore": ["La CPU è testata?"],
  "sintesi": "..."
}
```

Il campo `fonte` distingue un dato letto nell'annuncio, preso dai dati di riferimento o ricordato dal modello. Vale la pena guardarlo.

## Limiti noti

- **Numeri ricordati dal modello.** Valori come PassMark e TDP, se non stanno nell'annuncio, Groq li prende dalla propria memoria, e lì può sbagliare con sicurezza. Il prompt gli chiede di dichiarare `fonte: conoscenza` e di mettere `incerto` quando non è sicuro, ma per soglie strette come "single thread ≥ 1845" questo non basta. La soluzione è `dati_riferimento`: compila `dati/cpu_riferimento.csv` con i valori verificati su cpubenchmark.net per le CPU plausibili e attiva la riga nel file della caccia. Poche decine di righe coprono una piattaforma.
- **Le soglie decidono se esistono risultati.** Se nessuna CPU della piattaforma soddisfa insieme TDP e punteggi, la caccia gira senza mai trovare nulla. Con la tabella di riferimento compilata te ne accorgi prima di lanciarla.
- **Aste.** Il prezzo è l'offerta corrente, non quello finale: un'asta a 20 € che chiuderà a 120 € passa il filtro di budget. Il flag `asta` viene passato a Groq e salvato nel DB.
- **Annunci con varianti** (item group): il dettaglio non è sempre disponibile. La verifica parte comunque, con il solo riepilogo.
- **Stesso annuncio su più marketplace**: deduplicato per ID legacy, tenendo il totale più basso.
- **Descrizioni troncate** a `SCOVATORE_DESC_MAX_CHARS` per contenere i token. Se un venditore scrive i sintomi in fondo a una descrizione lunga, Groq non li vede.

## Struttura

```
scovatore/
  config.py     configurazione da .env
  hunt.py       schema e validazione delle cacce YAML
  ebay.py       client Browse API (OAuth, search, getItem, filtri)
  llm.py        client OpenAI-compatible con limitatore di token ed estrazione JSON robusta
  prompts.py    i tre prompt (piano, scrematura, verifica): si ritoccano qui
  pipeline.py   orchestrazione di un giro
  db.py         SQLite: piani, esecuzioni, annunci, verdetti
  notify.py     ntfy
  web.py        interfaccia web in sola lettura (solo libreria standard)
  cli.py        comandi
cacce/          definizioni delle cacce
dati/           dati di riferimento per Groq
data/           database (creato al primo avvio, non versionato)
tests/          test con eBay e LLM finti (httpx.MockTransport)
```

## Test

```bash
pip install pytest
python -m pytest -q
```

I test non chiamano servizi esterni: eBay, Palantir, Groq e ntfy sono simulati. Coprono filtri, validazione delle cacce, alias `ue` e filtro paesi, tetto di ricerche, migrazione del DB, pagine e token dell'interfaccia web, parsing JSON sporco, filtri locali, pipeline completa con cache di piano, scrematura e verifica, fallback senza `response_format`, notifiche e budget giornaliero eBay.

## Integrazione con GILPA (prossimo passo)

Il DB è autonomo (`data/scovatore.db`). GILPA può usare le API JSON di `scovatore-web` (`/api/caccia/<nome>`, `/api/giri`) per una schermata "Cacce attive". Più avanti, l'intent di `/api/chat` "cercami un..." può generare direttamente il file YAML di una caccia.
