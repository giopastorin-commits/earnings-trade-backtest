# D1_NONEWS_REVERSAL_V1 — Protocollo pre-registrato

**Stato:** congelato prima di qualunque calcolo di rendimenti. Al congelamento
sono stati eseguiti solo controlli di fattibilità su QuantConnect che
restituiscono conteggi e date: presenza dei titoli cessati (SIVB, FRC, BBBY),
498 costituenti SPY al 2010-01-04, 8 filing 8-K AAPL nel 2009 con i campi
`filingdate`, `madeavailableat`, `items`, `formtype`. Nessun rendimento, AR o
statistica è stato calcolato.

**Fonte della bozza:** documento "Scanner Research – Fase 2: audit scientifico
e programma a 90 giorni" (22/11/2026), Parte E. Questo file la trascrive e la
adatta ai dati QuantConnect. Le scelte nuove rispetto alla bozza sono marcate
**[Adattamento]**. Prevale questo file.

**Natura:** test di ricerca. Non autorizza operazioni reali.

## 1. Domanda e ipotesi

Tra i titoli dell'S&P 500, un forte calo **idiosincratico** in 3 sedute **senza
notizie societarie pubbliche identificabili** è seguito da un extra-rendimento
positivo nelle 5 sedute successive? È maggiore di quello degli shock
comparabili accompagnati da notizie? Copre i costi di un investitore retail?

- **H1 primaria (meccanismo):** E[AR | shock, NoNews] − E[AR | shock, News] > 0.
- **H1 operativa:** E[AR | shock, NoNews] > costo di andata e ritorno.
- **H0:** differenza ≤ 0, oppure AR NoNews ≤ costo.
- **Secondarie** (correzione di Holm al 5%; nessuna può salvare una primaria
  fallita):
  - **S0, baseline semplice:** reversal senza filtro notizie. D1 deve batterla
    al netto dei costi, altrimenti il filtro non serve.
  - **S1, simmetria:** i vincitori senza notizie (z ≥ +2,5) sottoperformano.
    Solo verifica di meccanismo, non negoziabile long-only.
  - **S2, discriminante HYP-04:** tra gli shock con notizia sugli utili (8-K
    item 2.02), confronto tra miglioramento e peggioramento dell'EPS diluito
    su base annua. **[Adattamento]** Fonte: fondamentali QuantConnect
    (Morningstar) disponibili alla data t. Diagnostica non negoziabile. Se i
    dati non sono disponibili, S2 = NOT ESTIMABLE.
  - **S3, HYP-12:** curva di decadimento con ingresso a t+1 apertura, t+1
    chiusura, t+2, t+3, t+5 (chiusure), uscita sempre a 5 sedute
    dall'ingresso.

## 2. Piattaforma e dati [Adattamento]

- **Piattaforma:** QuantConnect (LEAN), piano gratuito. Si registrano: versione
  LEAN, ID progetto e backtest, SHA del commit del codice, data e ora di ogni
  esecuzione.
- **Prezzi:** US Equity giornalieri QuantConnect, titoli cessati inclusi.
  - Rendimenti da prezzi **rettificati per split e dividendi** (total return).
  - Filtro di prezzo e controvalore da prezzi **non rettificati** (raw).
- **Universo:** costituenti dell'ETF SPY alla data t (dataset ETF Constituents
  di QuantConnect), proxy dichiarata dell'S&P 500.
- **Notizie:** dataset US SEC Filings, `SECReport8K` collegato al titolo.
- **Settore:** classificazione settoriale Morningstar dei fondamentali
  QuantConnect alla data t, mappata sugli ETF SPDR:
  - Technology → XLK; Financial Services → XLF; Healthcare → XLV;
    Consumer Cyclical → XLY; Consumer Defensive → XLP; Energy → XLE;
    Industrials → XLI; Basic Materials → XLB; Utilities → XLU;
  - Real Estate → XLRE dal 2015-10-08, prima XLF;
  - Communication Services → XLC dal 2018-06-19, prima XLK.
  - Titolo senza settore alla data t → escluso (SECTOR_MISSING).
- I dati restano nel cloud QuantConnect. Si esportano solo tabelle a livello
  di evento (identificativi, flag, AR) e aggregati, con SHA-256.

## 3. Universo, periodo e filtri

- Membri SPY alla data t, dal 2010-01-01 al 2025-12-31.
- Filtri pre-evento, calcolati con dati fino a t: prezzo raw ≥ 5 USD;
  almeno 252 sedute di storia; controvalore medio raw a 20 giorni
  ≥ 20 milioni USD.
- Titoli cessati inclusi (verificato nella fattibilità).

## 4. Formula del segnale

- r = rendimento giornaliero total return; s(i) = ETF settoriale del titolo
  (sezione 2).
- βᵢ = OLS di rᵢ su rₛ sulle sedute [t−252, t−22], ridotto verso 1:
  β = 0,5·β_OLS + 0,5.
- Residuo eᵢ,d = rᵢ,d − βᵢ·rₛ,d; σᵢ = deviazione standard di e su [t−63, t−4].
- zᵢ,t = (eᵢ,t−2 + eᵢ,t−1 + eᵢ,t) / (σᵢ·√3).
- **Evento:** z ≤ −2,5. Per ogni titolo un nuovo evento è ammesso solo dopo
  l'uscita dal precedente.
- **Notizia (proxy):** News = 1 se esiste un 8-K del titolo (qualsiasi item,
  inclusi 2.02) reso disponibile tra le 16:00 ET di t−3 e le 16:00 ET di t;
  altrimenti News = 0. **[Adattamento]** Timestamp: `madeavailableat`,
  convertito in America/New_York, purché la calibrazione (sezione 7, passo 1)
  lo validi. Se la calibrazione lo invalida, si usa `filingdate` con la regola
  "data di deposito in {t−2, t−1, t}". La scelta è registrata prima di
  qualunque AR.
- Diagnostica non negoziabile: quota di eventi NoNews con un 8-K in
  [t+1, t+4], cioè notizie depositate in ritardo.
- **Ingresso:** apertura di t+1. **Uscita:** chiusura di t+5.
- **AR** = rendimento del titolo − β·rendimento dell'ETF settoriale, sullo
  stesso intervallo apertura t+1 → chiusura t+5. Benchmark secondari:
  universo SPY equipesato e SPY.

## 5. Barre non negoziabili e titoli cessati [Adattamento]

La fattibilità ha mostrato barre con volume zero e prezzo fermo dopo sospensioni
e delisting (SIVB, FRC). Regole fissate prima dei dati:

1. Una barra con volume = 0 o prezzo mancante **non è un prezzo**. Non entra
   in β, σ, z, rendimenti, filtri o esecuzione.
2. Finestre di stima: se più del 10% delle sedute di [t−252, t−22] o di
   [t−63, t−4] non ha una barra valida → evento escluso
   (INSUFFICIENT_VALID_BARS).
3. Ingresso: serve una barra valida a t+1. Altrimenti evento
   NOT_ENTERABLE: contato, senza AR, non sostituito.
4. Uscita: se la barra di t+5 non è valida, si esce alla chiusura dell'ultima
   barra valida in [t+1, t+5].
5. Se il titolo è delistato o non torna a scambiare entro t+5, si applica
   all'ultima chiusura valida una penalità di delisting del **−30%**.
   Sensibilità riportate: 0% e −100%.

## 6. Costi

Andata e ritorno di riferimento **0,55%**: commissione Fineco 0,19% × 2, più
2 × (mezzo spread 3 bp + slippage in apertura 5 bp). Sensibilità: 0,29% e
1,10% (costi raddoppiati).

## 7. Campioni e ordine di esecuzione

1. **Calibrazione, senza outcome:**
   - controllo di date e timestamp, e validazione di `madeavailableat`
     contro l'orario di accettazione EDGAR su 30 filing estratti a caso (seed
     20261123): valido se almeno il 98% coincide entro 1 ora;
   - rendimenti giornalieri anomali dovuti a corporate action;
   - quota di eventi da titoli poi cessati;
   - copertura del flag notizie;
   - verifica manuale di 30 eventi NoNews estratti a caso (seed 20261123) per
     stimare il tasso di falsi "no news".

   Nessun AR viene calcolato. **Interruzione:** se più del 2% degli eventi ha
   timestamp sbagliati o errori da corporate action, si correggono i dati
   prima di guardare un solo outcome.
2. **Fase 1, 2010–2021:** esecuzione **unica** con i parametri fissati →
   decisione go/no-go.
3. **Fase 2, holdout 2022–2025:** aperto una sola volta, solo se la Fase 1
   passa.
4. **Fase 3, prospettica:** regole congelate, lista dei trigger congelata con
   hash prima dell'apertura (ingresso t+1 apertura → uscita t+5 chiusura,
   append-only).

Qualunque modifica dopo la Fase 1 genera un protocollo V2. A quel punto
l'holdout 2022–2025 perde valore confermativo e la conferma può venire solo
dalla fase prospettica.

## 8. Inferenza

- **A livello di evento:** differenza di medie NoNews − News, con errore
  standard clusterizzato per settimana di calendario di t; sensibilità a due
  vie settimana × titolo. p-value da wild cluster bootstrap-t per settimana:
  Rademacher, 9.999 repliche, seed **20261123**.
- **Portafoglio in tempo di calendario:** ogni giorno, portafoglio equipesato
  delle posizioni NoNews aperte; rendimento giornaliero coperto con il
  settore. Errori standard Newey-West (5 ritardi); block bootstrap circolare
  sui rendimenti settimanali (blocchi di 4 settimane, 10.000 repliche, seed
  20261123). Alpha rispetto ai fattori giornalieri della Kenneth French Data
  Library, solo diagnostico.
- **Portafoglio operativo a 5 slot:** massimo 5 posizioni contemporanee
  scelte per z più negativo, capitale non investito a rendimento zero. È la
  metrica operativa principale.
- **Concentrazione:** quota dell'AR totale dovuta all'1% e al 5% migliore
  degli eventi; risultati senza il miglior 1%; esclusione di un anno e di un
  settore alla volta; mediana; quota di anni positivi.

## 9. Placebo

1. Date casuali per lo stesso titolo nello stesso mese (seed 20261123): AR
   atteso nullo.
2. Flag notizie permutato tra eventi della stessa settimana: la differenza
   deve sparire.
3. Ingresso ritardato di 10 sedute: l'effetto deve sparire.

## 10. Criteri di successo e fallimento

**SUCCESSO in Fase 1**, tutte le condizioni:

1. differenza NoNews − News > 0 con t ≥ 3 e p bootstrap < 0,005;
2. AR medio NoNews **al netto di 0,55%** > 0 con t ≥ 2;
3. portafoglio a 5 slot con rendimento netto positivo in almeno 7 anni su 12;
4. AR lordo NoNews > 0 anche senza il miglior 1% degli eventi;
5. nessun placebo significativo (p > 0,10);
6. D1 batte la baseline semplice S0 al netto dei costi.

**CONFERMA in Fase 2:** stesso segno per differenza e AR netto; AR netto ≥ 50%
della stima di Fase 1; p unilaterale < 0,05 per AR netto > 0.

**FALLIMENTO:**

- AR lordo NoNews < 0,55% → "effetto presente ma non negoziabile ai costi
  retail": D1 archiviata come strategia;
- differenza ≤ 0 → anche il meccanismo è archiviato.

**INCONCLUDENTE:** tutti gli altri casi. Nessuna estensione; al massimo un
protocollo V2 pre-registrato, con conferma solo prospettica.

## 11. Limiti dichiarati in anticipo

- Universo SPY come proxy dell'S&P 500; settore Morningstar come proxy GICS.
- Il flag notizie considera solo gli 8-K: comunicati stampa senza 8-K, notizie
  di settore o macro non sono rilevati.
- Penalità di delisting convenzionale (sezione 5).
- Il premio da reversal si concentra nei periodi di stress: rendimenti
  irregolari attesi.

## 12. Controllo delle modifiche

Qualunque modifica sostanziale dopo il congelamento richiede un nuovo
protocollo (V2) con un nuovo commit. Le griglie di sensibilità si riportano
intere, mai scelte. Nel dubbio, si emette una nuova versione.
