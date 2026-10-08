# Scovatore

Cacciatore di annunci per NASGUL (eBay, Vinted, Subito.it). Gli descrivi a parole cosa cerchi, senza marca e modello, e lui:

1. fa generare a **Palantir** o a **Groq** (a scelta, `SCOVATORE_PLAN_LLM`) le query di ricerca (sinonimi, varianti, più lingue);
2. lancia le ricerche sulle fonti attive della caccia: la **Browse API** di eBay con i filtri strutturati (prezzo, area geografica, condizione...) e, se richiesti, **Vinted** e **Subito.it**;
3. scarta in locale quello che non rientra (budget con spedizione, valuta, parole escluse, feedback);
4. fa scremare i titoli a **Palantir**, a blocchi (si / forse / no);
5. manda i sopravvissuti, con descrizione e specifiche complete, a **Groq** per la verifica dei requisiti avanzati;
6. salva tutto in SQLite, stila una classifica e, se configurato, notifica via **ntfy** i nuovi annunci sopra soglia;
7. mostra i risultati in una piccola **interfaccia web** (servizio `scovatore-web`, porta 8482).

eBay usa l'API ufficiale, gratuita con un account developer. Vinted e Subito.it non hanno un'API pubblica: si leggono gli stessi endpoint dei loro siti, con tutti i limiti descritti in [Vinted e Subito](#vinted-e-subito). Ogni fonte si attiva per caccia con il campo `fonti`.

```
 cacce/*.yaml
      |
      v
 [Palantir] piano ---> query per lingua (in cache finche' la ricerca non cambia)
      |
      v
 [fonti] eBay Browse API / Vinted / Subito.it, solo quelle in `fonti`
      ricerca x marketplace (o dominio, o regione) x query
      |
      v
 filtri locali: totale con spedizione, valuta, parole escluse, feedback
      |
      v
 [Palantir] scrematura titoli a blocchi ---> "no" scartati
      |
      v
 [fonte] dettaglio annuncio  +  [Groq] verifica requisiti avanzati ---> punteggio 0-100
      |
      v
 SQLite  ->  classifica / CSV / notifica ntfy
```

## Divisione dei compiti

| Cosa | Chi | Perché |
|---|---|---|
| Prezzo, valuta, area geografica, paese di consegna, condizione, formato, venditori | eBay (parametri YAML) | eBay li filtra meglio e gratis |
| Budget con spedizione inclusa, parole escluse, feedback minimo, prezzo su Subito.it | Scovatore in locale | deterministico, zero token |
| Query sinonime a partire dagli elementi chiave | Palantir o Groq (`SCOVATORE_PLAN_LLM`) | una sola chiamata per caccia, poi in cache: con Groq costa poche centinaia di token e sbaglia meno |
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

Il container gira in `loop`: ogni 10 minuti controlla quali cacce sono scadute (campo `ogni_minuti`) e le esegue; ogni 10 secondi controlla anche le richieste "Riesegui ora" arrivate dall'interfaccia web. Per aggiungere o modificare una caccia basta toccare il file YAML (o usare l'editor dell'interfaccia web), senza riavviare. Il servizio `scovatore` monta `cacce/` e `dati/` in sola lettura; il servizio web monta `cacce/` in scrittura, perché è lui che salva le modifiche dell'editor.

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
python -m scovatore esegui ampli-guasto --fonte vinted   # solo una fonte (ripetibile: --fonte vinted --fonte subito)
python -m scovatore tutte [--forza]                   # tutte le cacce attive scadute
python -m scovatore loop --intervallo 10              # modalità servizio
python -m scovatore risultati ampli-guasto --min 60 --csv ampli.csv
python -m scovatore risultati ampli-guasto --tutti    # anche i non verificati
```

`-v` attiva il log di debug (mostra anche il motivo di ogni scarto locale).

### Interfaccia web

Il compose avvia anche `scovatore-web`, che lavora sullo stesso database del servizio:

- `http://nasgul:8482/`: riepilogo delle cacce (conformi, verificati, in attesa, ultimo giro ed eventuali errori);
- **editor delle cacce** (scheda in home o pagina della caccia, link **Modifica**): mostra e modifica il file YAML della caccia come testo, con un riepilogo (stato, fonti, prezzo, lingue). Al salvataggio il file viene controllato con la stessa validazione del caricamento: se c'è un errore (YAML rotto, campo sconosciuto, valore non valido) non salva e mostra il motivo, con il testo che hai scritto ancora nell'editor. La versione precedente finisce in `cacce/.storico/` (ultime 20 per caccia) e dalla pagina si può ricaricare nell'editor. Il nome della caccia non si cambia (dati e giri sono legati al nome), e se il file è cambiato nel frattempo (altra modifica, `git pull`) il salvataggio viene rifiutato invece di sovrascriverla. `dati_riferimento` può puntare solo dentro `dati/`. Una modifica vale dal giro successivo. **Serve `SCOVATORE_WEB_TOKEN`**: senza token le pagine si vedono ma non si salva;
- **nuova caccia** (pulsante in home): parte da `cacce/esempio-fonti.yaml`, spenta; il nome del file si ricava dal campo `nome`;
- **Accendi / Spegni** (sulla scheda in home): cambia solo la riga `attiva:` del file, lasciando commenti e resto intatti;
- **cacce spente ed eliminate**: la home le mostra in sezioni separate, con un'etichetta e un colore (ambra le spente, rosso le eliminate), e ognuna ha il pulsante **Elimina dati...**. *Spenta* = nel file YAML c'è `attiva: false` (si può ancora rieseguire a mano). *Eliminata* = il file YAML non c'è più ma nel database restano i dati (non si può rieseguire). Un file YAML illegibile o la cartella `cacce/` non montata non vengono mai scambiati per una caccia eliminata;
- **Elimina i dati dal database** (pulsante sulla scheda in home, o link nella pagina della caccia): apre una pagina di conferma che mostra quanti annunci, giri e piani verrebbero cancellati e chiede di scrivere il nome della caccia. Cancella solo il database, **mai il file YAML**: una caccia spenta resta definita e, riaccesa, riparte da zero. Una caccia attiva non si può eliminare, né se ha una richiesta in coda o un giro in corso. I giri di oggi restano, perché da quelli si calcola il tetto giornaliero di chiamate eBay. Gli annunci corretti a mano si perdono insieme agli altri;
- `/caccia/<nome>`: classifica filtrabile per esito, punteggio minimo, fonte, paese, periodo, con ordinamento per punteggio, prezzo o novità. Ogni annuncio ha il dettaglio della verifica (requisiti, fonte del dato, rischi, domande al venditore) e il motivo della scrematura;
- **interventi manuali**, su un annuncio (pulsanti sotto la scheda) o su più annunci (caselle + barra in alto, con nota facoltativa):
  - **Conforme / Incerto / Non conforme**: l'esito dell'operatore prevale su quello del modello, che resta visibile nel dettaglio ("il modello diceva: ..."). Groq non lo sovrascrive più, nemmeno se il prezzo cambia; **Togli correzione** torna al giudizio del modello;
  - **Riverifica**: rimette l'annuncio in coda per Groq al prossimo giro, anche se la scrematura lo aveva scartato (serve che l'annuncio sia ancora online e ricompaia nelle ricerche);
  - **Elimina**: toglie l'annuncio dalla caccia. Non viene cancellato dal DB, altrimenti eBay lo restituirebbe al giro dopo e ripartirebbero scrematura e verifica: resta marcato, la pipeline lo ignora e non lo notifica, e si ripristina dalla vista **Eliminati**;
  - la vista **Corretti a mano** raccoglie gli interventi: è il materiale giusto per capire dove sbagliano i prompt;
- **Riesegui** (pagina `/giri`, sulla riga più recente di ogni caccia): forza un giro senza aspettare `ogni_minuti`, anche per una caccia spenta. Si sceglie cosa rieseguire (tutta la caccia o una sola fonte) e se rigenerare le query con l'LLM. Il servizio web non esegue nulla: scrive la richiesta nel database e il container `scovatore` la prende entro 10 secondi. La riga mostra "in coda" / "giro in corso" e la pagina si aggiorna da sola; una richiesta già in coda non si duplica. Le cacce senza giri recenti hanno una riga apposta, quelle eliminate (senza file) non sono eseguibili. Un giro parziale (una sola fonte) non sposta la scadenza di `ogni_minuti`;
- `/giri`: gli ultimi giri di tutte le cacce con durata di ogni fase, contatori ed errori;
- `/api/caccia/<nome>` e `/api/giri`: gli stessi dati in JSON (accettano gli stessi parametri della pagina), utili per GILPA.

Senza `SCOVATORE_WEB_TOKEN` non c'è protezione: va bene finché la porta resta in LAN (le azioni accettano solo form inviati dalla pagina stessa, non da altri siti). Con il token impostato si apre una volta `http://nasgul:8482/?token=...` e il browser lo ricorda. Da riga di comando: `python -m scovatore web [--porta 8482]`.

### Log

A livello `INFO` (default) il log dice sempre cosa sta facendo: inizio e fine di ogni fase con la durata, ogni ricerca (marketplace, query, risultati, quanti mai visti nel giro), la provenienza degli annunci per paese, gli scarti dei filtri locali raggruppati per motivo, ogni blocco di scrematura con il conteggio si/forse/no, ogni verifica con esito e punteggio, e un riepilogo a fine giro. Nel `loop` dice anche quando scade ogni caccia e a che ora c'è il prossimo controllo. Le chiamate LLM oltre i 60 secondi vengono segnalate.

Con `-v` o `SCOVATORE_LOG_LEVEL=DEBUG` si aggiungono il motivo di ogni singolo scarto, le query del piano, i parametri di ogni chiamata eBay e tempi e token di ogni chiamata LLM.

### Chi genera il piano

`SCOVATORE_PLAN_LLM=palantir` (default) o `groq`. Il piano è una sola chiamata per caccia e resta in cache, quindi farlo con Groq costa poco; la scrematura dei titoli resta comunque a Palantir. Se il modello scelto non è configurato, Scovatore usa l'altro e lo scrive nel log. Cambiare impostazione rigenera il piano al giro successivo.

Qualunque sia il modello, le query fatte solo di parole di stato o generiche ("non funzionante", "defekt", "for parts", "usato"...) vengono scartate prima di arrivare a eBay e segnalate nel log: da sole pescano in tutte le categorie. Il pianificatore riceve anche i filtri che eBay applica già: se la caccia chiede solo oggetti guasti, sa che le parole di stato sono superflue e concentra le query sul tipo di oggetto.

**Consiglio per la prima volta**: lancia `piano` e guarda le query prima di `esegui`. Se Palantir produce query troppo generiche o troppo specifiche, correggi `ricerca` oppure aggiungi `query_extra`.

## Definire una caccia

Un file YAML in `cacce/`. Nel repository c'è solo il modello `cacce/esempio-fonti.yaml`: le cacce vere sono configurazione personale, restano sul server e sono ignorate da git (`.gitignore`). Si creano e si modificano dall'interfaccia web o a mano.

```yaml
nome: ampli-guasto
attiva: true
ogni_minuti: 240

ebay:                          # tutto quello che eBay sa filtrare
  marketplaces: [EBAY_IT]      # si cerca su ebay.it
  paesi_ammessi: ue            # oggetto situato in uno dei 27 paesi UE
  prezzo_max: 150
  spedizione_inclusa: true
  regione: EUROPEAN_UNION
  consegna_paese: IT
  condizioni: [guasto]

fonti: [ebay]                  # facoltativo: ebay, vinted, subito (default: solo eBay)

ricerca: >                     # per Palantir: cosa cercare
  Amplificatore audio hi-fi guasto o non funzionante, venduto per ricambi.

requisiti_avanzati: |          # per Groq: cosa verificare sul dettaglio
  - Marca di buona qualità costruttiva.
  - Sintomi compatibili con un guasto di alimentazione, non dei finali.
```

### Sezione `ebay`

| Campo | Default | Filtro eBay / effetto |
|---|---|---|
| `marketplaces` | `[EBAY_IT]` | dove si cerca: ogni marketplace riceve le query di tutte le `lingue`. `ue` = `EBAY_IT, DE, FR, ES, NL, BE, AT, IE, PL`, sconsigliato (vedi sotto) |
| `paesi_ammessi` | `ue` | controllo locale sul paese in cui si trova l'oggetto: `ue` (i 27 stati membri), una lista ISO (`[IT, DE, LT]`, anche `[ue, CH]`) oppure `tutti` |
| `max_ricerche` | `150` | tetto di ricerche (query x marketplace) per giro; se taglia, taglia le query meno importanti su tutti i marketplace |
| `valuta` | `EUR` | `priceCurrency`; gli annunci in altra valuta vengono scartati |
| `prezzo_min`, `prezzo_max` | nessuno | `price:[min..max]` |
| `spedizione_inclusa` | `true` | il budget vale su prezzo + spedizione (controllo locale) |
| `spedizione_ignota` | `scarta_estero` | annunci senza costo di spedizione verso `consegna_paese`: `scarta_estero` li scarta se l'oggetto è all'estero (quasi sempre non spedisce qui) e li tiene se è in Italia (può essere ritiro a mano); oppure `tieni` / `scarta` |
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
| `spedizione_max` | nessuno | tetto alla sola spedizione, in euro (controllo locale, solo eBay) |
| `feedback_minimo` | nessuno | % minima di feedback del venditore (controllo locale) |

#### Tutta la UE, cercando solo su ebay.it

Su ebay.it compaiono anche gli annunci dei venditori degli altri paesi UE che spediscono in Italia. Per questo si cerca su un solo marketplace, con tre filtri:

- `regione: EUROPEAN_UNION` chiede a eBay solo oggetti situati nella UE;
- `consegna_paese: IT` (con il contesto acquirente `EBAY_BUYER_COUNTRY`) solo annunci che spediscono in Italia;
- `paesi_ammessi: ue` ricontrolla in locale il paese dell'annuncio contro i 27 stati membri: Regno Unito, Svizzera e Norvegia costano dogana e IVA all'import.

Quello che resta si gioca sulle **lingue**: un venditore lituano o polacco scrive il titolo nella sua lingua anche quando l'annuncio è visibile su ebay.it. Per questo tutte le query del piano (italiano, inglese, tedesco, francese, spagnolo, olandese, polacco) partono su ebay.it.

Cercare anche su ebay.de & co. (`marketplaces: ue`) resta possibile, ma porta annunci che spesso **non spediscono in Italia** e link su siti dove non sei loggato. Scovatore li filtra in due punti:

1. **filtri locali**: un annuncio dall'estero senza costo di spedizione per l'Italia viene scartato (`spedizione_ignota: scarta_estero`, default);
2. **prima della verifica**: il dettaglio eBay dice dove spedisce il venditore (`shipToLocations`). Se esclude l'Italia, o ammette solo un elenco di paesi senza l'Italia, l'annuncio finisce tra gli scartati con motivo "non spedisce in IT" e Groq non lo vede. Nel dubbio l'annuncio passa.

**Link**: interfaccia, notifiche e CSV puntano sempre a `https://www.ebay.it/itm/<id>` (`SCOVATORE_LINK_DOMAIN`), qualunque sia il marketplace dove è stato trovato: lo stesso ID vale su tutti i siti eBay e su quello italiano vedi subito spedizione e prezzo per l'Italia.

**Costo in chiamate.** Con un solo marketplace, 7 lingue da 4 query e 2 `query_extra` un giro fa circa 30 ricerche più fino a 25 dettagli: con una caccia ogni 3 ore, intorno alle 450 chiamate al giorno. Cambiare le lingue rigenera il piano al primo giro.

Nota: secondo la documentazione eBay, `buyingOptions` funziona in modo affidabile solo insieme a una categoria foglia. Senza `categorie` è meglio lasciarlo vuoto: altrimenti si rischiano risultati mancanti.

### Fonti: `fonti`, sezione `vinted`, sezione `subito`

`fonti` elenca le ricerche attive per la caccia (`ebay`, `vinted`, `subito`; accetta anche `subito.it`). Se manca vale `[ebay]`, quindi le cacce esistenti non cambiano. Una fonte non elencata non gira e non consuma richieste. Esempio completo, disattivato: `cacce/esempio-fonti.yaml`.

**Prezzo**: `prezzo_min` / `prezzo_max` di `vinted` e `subito` valgono per quella fonte; se omessi valgono quelli della sezione `ebay`.

#### Sezione `vinted`

| Campo | Default | Effetto |
|---|---|---|
| `domini` | `[it]` | siti Vinted da interrogare: `it, fr, de, es, nl, pl, be, at, lu, pt, lt, cz`. Ogni dominio riceve le query nella sua lingua |
| `valuta` | `EUR` | gli annunci in altra valuta vengono scartati |
| `prezzo_min`, `prezzo_max` | quelli di `ebay` | `price_from` / `price_to` |
| `spedizione_stimata` | nessuna | sommata al prezzo per il budget. Vinted non espone la spedizione nell'elenco: senza questo valore si guarda solo il prezzo |
| `condizioni` | tutte | stato dichiarato dal venditore: `nuovo_cartellino`, `nuovo`, `ottimo`, `buono`, `discreto`, oppure ID numerici (`status_ids`) |
| `categorie` | nessuna | `catalog_ids` di Vinted |
| `ordinamento` | `newest_first` | `relevance`, `price_low_to_high`, `price_high_to_low` |
| `max_risultati_per_query` | `48` | massimo 96 per pagina |
| `max_ricerche` | `20` | tetto di richieste (query x dominio) per giro |

#### Sezione `subito`

| Campo | Default | Effetto |
|---|---|---|
| `regione` | `italia` | slug della regione nell'URL (`piemonte`, `lombardia`...). Molti annunci sono solo a ritiro |
| `categoria` | `usato` | slug della categoria (`informatica`, `audio-video`...) |
| `valuta` | `EUR` | etichetta della valuta |
| `prezzo_min`, `prezzo_max` | quelli di `ebay` | **solo controllo locale**: nell'URL di Subito i filtri di prezzo sono indici di fasce |
| `ordinamento` | `datedesc` | `priceasc`, `pricedesc` |
| `max_risultati_per_query` | `50` | circa 30 annunci per pagina |
| `max_ricerche` | `12` | tetto di richieste per giro |

Spedizione su Subito: non esiste nell'elenco, il totale è il prezzo.

### Altri campi

| Campo | Default | Uso |
|---|---|---|
| `ricerca` | obbligatorio | testo libero per Palantir: cosa cercare. Solo gli elementi che finiscono nei titoli degli annunci |
| `requisiti_avanzati` | vuoto | testo libero per Groq: i criteri che richiedono conoscenza o lettura della descrizione |
| `query_extra` | `[]` | query scritte a mano, usate su tutte le fonti insieme a quelle generate |
| `parole_escluse` | `[]` | scarto locale sul titolo, a parola intera. Si sommano a quelle proposte da Palantir |
| `screening` | `true` | scrematura dei titoli con Palantir. Con `false` va tutto a Groq (attenzione ai token) |
| `dati_riferimento` | nessuno | file di testo o CSV, percorso relativo al file YAML, passato a Groq come fonte da preferire alla sua memoria |
| `lingue` | `ue` | lingue delle query: `ue` = `it, en, de, fr, es, nl, pl`, oppure una lista. Si aggiungono sempre l'inglese e la lingua dei marketplace |
| `max_query_per_lingua` | `4` | tetto sulle query generate per ciascuna lingua |
| `attiva` | `true` | le cacce disattivate non girano in `loop` e `tutte` |
| `ogni_minuti` | `180` | intervallo minimo fra due esecuzioni automatiche |

## Cosa rigira e cosa no

Ogni giro ricerca sempre su tutte le fonti attive, ma i passaggi LLM si ripetono solo quando serve:

- **piano**: uno per fonte (per Vinted e Subito le parole di stato restano nelle query, perché il sito non le filtra; se due fonti hanno lo stesso piano la chiamata è una sola). In cache finché non cambiano `ricerca`, marketplace (lingue), `max_query_per_lingua`, condizioni, il modello che lo genera o il prompt. Forzabile con `--rigenera` / `--rigenera-piano` o dalla casella dell'interfaccia web;
- **scrematura**: una volta per annuncio;
- **verifica Groq**: una volta per annuncio, ripetuta se cambiano `ricerca`, `requisiti_avanzati` o `dati_riferimento`, oppure se il prezzo totale si muove di oltre il 5% (aste, ribassi);
- **notifica**: una volta per annuncio, mai per quelli eliminati o corretti a mano.

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

## Vinted e Subito

Sono la parte fragile di Scovatore. Leggi prima di affidarti ai risultati.

- **Non sono API ufficiali.** Vinted si interroga con l'endpoint JSON che usa il suo sito: da settembre 2026 è `api.vinted.<paese>/svc-catalogue/items` con token anonimo `Bearer` (il vecchio `www.vinted.<paese>/api/v2/catalog/items` risponde 404). Subito.it con il JSON `__NEXT_DATA__` incorporato nella pagina dei risultati. Possono cambiare senza preavviso, come è già successo. In quel caso la fonte segnala l'errore ("formato cambiato", "pagina di blocco") invece di restituire zero risultati in silenzio, e il giro prosegue con le altre fonti.
- **Condizioni d'uso.** Entrambi i siti vietano o limitano la lettura automatica. Scovatore lavora in modo educato (pausa `SCOVATORE_SCRAPE_DELAY` fra le richieste, poche ricerche per giro, nessun login) e **non aggira i blocchi**: davanti a un 403/429 la fonte si ferma per quel giro, e dopo 3 errori consecutivi pure. Resta una tua valutazione se e quanto usarli; per uso personale e a bassa frequenza il rischio pratico è un blocco temporaneo dell'IP.
- **Subito blocca i client automatici.** Spesso risponde 403 alle richieste che non vengono da un browser vero. Scovatore non cerca di aggirare la protezione: se il 403 è costante, togli `subito` dal campo `fonti` della caccia.
- **Cosa non è verificato.** Vinted è stato adattato alla nuova API sulla base di descrizioni di terzi e provato solo su risposte simulate. Da confermare al primo giro reale: il formato dei filtri con più valori (`attribute_ids[status]`, `attribute_ids[catalog]`, qui separati da virgola) e i campi `item_box` di stato e taglia. Lancia `esegui --fonte vinted -v` (o "Riesegui" dalla pagina Giri) e guarda il log.
- **Spedizione sconosciuta.** Né Vinted né Subito la danno nell'elenco: su Vinted si usa `spedizione_stimata`, su Subito il prezzo è il totale (e spesso si ritira a mano).
- **Nessuna garanzia di stato.** Il guasto non è filtrabile dal sito: lo giudica Groq dalla descrizione, quindi scrivi in `requisiti_avanzati` di scartare gli oggetti funzionanti.
- **ID.** Gli annunci di queste fonti hanno ID con prefisso (`vinted:123`, `subito:456`) e il link punta all'annuncio originale; gli ID eBay restano quelli di sempre.
- **Costo.** Ogni annuncio verificato richiede una richiesta di dettaglio alla pagina del sito, oltre al token Groq.

## Struttura

```
scovatore/
  config.py     configurazione da .env
  hunt.py       schema e validazione delle cacce YAML
  huntfiles.py  modifica sicura dei file YAML: validazione, storico, nuova caccia, attiva/spegni
  ebay.py       client Browse API (OAuth, search, getItem, filtri)
  sources/      fonti web senza API: base.py (pausa, blocchi, URL sicuri), vinted.py, subito.py
  llm.py        client OpenAI-compatible con limitatore di token ed estrazione JSON robusta
  prompts.py    i tre prompt (piano, scrematura, verifica): si ritoccano qui
  pipeline.py   orchestrazione di un giro
  db.py         SQLite: piani, esecuzioni, annunci, verdetti, richieste di riesecuzione
  notify.py     ntfy
  web.py        interfaccia web con interventi manuali (solo libreria standard)
  cli.py        comandi
cacce/          definizioni delle cacce (solo il modello e' nel repository; .storico/ = copie dell'editor)
dati/           dati di riferimento per Groq
data/           database (creato al primo avvio, non versionato)
tests/          test con eBay e LLM finti (httpx.MockTransport)
```

## Test

```bash
pip install pytest
python -m pytest -q
```

I test non chiamano servizi esterni: eBay, Vinted, Subito.it, Palantir, Groq e ntfy sono simulati. Coprono filtri, validazione delle cacce, alias `ue` e filtro paesi, tetto di ricerche, migrazione del DB, pagine e token dell'interfaccia web, parsing JSON sporco, filtri locali, pipeline completa con cache di piano, scrematura e verifica, fallback senza `response_format`, notifiche e budget giornaliero eBay, parsing e isolamento dei guasti delle fonti web, coda delle riesecuzioni dal web, editor dei file di caccia (validazione, storico, conflitti, percorsi ostili, token).

## Integrazione con GILPA (prossimo passo)

Il DB è autonomo (`data/scovatore.db`). GILPA può usare le API JSON di `scovatore-web` (`/api/caccia/<nome>`, `/api/giri`) per una schermata "Cacce attive". Più avanti, l'intent di `/api/chat` "cercami un..." può generare direttamente il file YAML di una caccia.
