# TRINITY/H2 — Protocollo statistico V2

## 1. Identità, stato e rapporto con V1

**Protocollo:** `H2_PROTOCOL_V2`

**Stato:** pre-registrato e congelato *prima* di qualunque analisi V2. Nessuna
relazione tra H2, volatilità pre-evento e outcome è stata calcolata o
ispezionata per redigere questo documento.

**Rapporto con V1:** `H2_PROTOCOL_V1.md` (SHA-256
`219c2f4243dfb7d16b9eeeaf55ca43de586be1a41cd898b675f6d7562f91f87d`, branch
`trinity-live-validation-v1`, commit `8221ff6a9e26811ca549bd17322d9026bca6815e`)
resta in vigore, invariato, con i suoi risultati. V2 non lo sostituisce, non
lo rietichetta e non ne reinterpreta l'esito. V2 è un protocollo nuovo,
richiesto dalla sezione "Change control" di V1 perché cambia l'analisi
primaria (aggiunta di un controllo) e il campione.

**Motivo del cambiamento:** l'analisi primaria V1 è una regressione di rango
con due soli parametri (intercetta e H2). L'outcome primario,
`absolute_return_pct_20`, dipende in larga parte dalla volatilità propria del
titolo. Se le aziende più volatili producono anche comunicati più "nuovi", V1
può rilevare un'associazione senza che H2 contenga informazione aggiuntiva.

**Natura dei risultati:** poiché l'esito di V1 è noto, l'analisi sul dataset V1
(Parte A) è un'analisi di **robustezza**, non una conferma. Solo la Parte C
(campione prospettico) ha valore confermativo.

**Nessuna autorizzazione al trading:** come V1, questo protocollo non definisce
né autorizza segnali, dimensionamento, ordini o strategie.

## 2. Domanda di ricerca e ipotesi

> H2 aggiunge capacità predittiva sull'ampiezza del movimento a +20 sedute
> rispetto alla sola volatilità storica pre-evento, su dati realmente fuori
> campione?

- **H0 (V2):** aggiungere H2 a un modello basato sulla sola volatilità
  pre-evento non riduce l'errore di previsione fuori campione.
- **H1 (V2):** il modello con H2 ha un errore assoluto medio fuori campione
  inferiore, e la riduzione raggiunge la soglia di interesse pratico
  (sezione 9).

Un coefficiente di H2 statisticamente significativo che non migliora l'errore
di previsione **non** costituisce supporto per H2A-V2.

## 3. Componenti congelati

Restano congelati e invariati i componenti di V1:

- Outcome Engine V0.2
- H2 TFIDF V0.1 (`h2_method == "TFIDF_COSINE_V1"`)
- Earnings Pairing Engine V0.2
- Experimental Dataset Builder V0.1

Si aggiunge un solo componente nuovo, definito qui e da implementare *prima* di
caricare qualunque outcome V2:

- **Pre-Event Covariates V0.1** (sezione 6). L'implementazione deve essere
  verificata con test sintetici contro questa specifica e congelata con il suo
  commit SHA prima dell'esecuzione.

## 4. Unità di analisi e criteri di ammissibilità

L'unità è un comunicato sugli utili corrente (`current_release_id`), presente
al massimo una volta. Valgono **tutti** i criteri del campione primario V1:

- `row_status == "INCLUDED"`;
- `pairing_quality == "EXACT"`;
- `h2_method == "TFIDF_COSINE_V1"`;
- `h2_novelty` non nullo;
- `outcome_status_20 == "COMPLETE"`;
- `absolute_return_pct_20` non nullo;
- `timing_ambiguous == false`;
- `reference_resolution != "AMBIGUOUS"`.

Criteri aggiuntivi V2:

- `sigma_pre_60` disponibile (sezione 6);
- `dollar_volume_pre_60` disponibile e strettamente positivo.

Nessun criterio usa il valore numerico di H2, delle covariate o degli outcome.
Le righe escluse restano nel dataset di audit con il motivo di esclusione.

**Universo:** lo stesso universo di emittenti usato per costruire il dataset
V1. Il file dell'universo e il suo SHA-256 vanno registrati all'esecuzione.

## 5. Campioni

I campioni si definiscono tramite `reference_session_date`, la seduta di
riferimento fissata dall'Outcome Engine V0.2: per `PRE_MARKET` è la seduta
precedente al giorno dell'annuncio, per `AFTER_MARKET` è la seduta del giorno
dell'annuncio. Le date di seduta sono quelle presenti nella serie di prezzi
usata dall'Outcome Engine.

**Istante di congelamento V2 (`T_freeze`):** l'orario del commit che introduce
questo file nel repository GitHub, insieme al suo SHA. Per costruzione è il
2026-11-18, prima dell'apertura NYSE. L'ultima seduta completata prima di
`T_freeze` (`S_last`) è quindi il **2026-11-17**.

**Regola di maturazione:** un evento è *maturato al congelamento* se la sua
seduta futura a +20 (`future_session_date` per l'orizzonte 20) è ≤ `S_last`.
Altrimenti è *non maturato*.

Con il calendario NYSE 2026, la seduta di riferimento il cui D20 coincide con
`S_last` è il 2026-10-20. Quindi:

- riferimento **≤ 2026-10-20** → maturato;
- riferimento **≥ 2026-10-21** → non maturato.

Queste date sono informative. In caso di discrepanza prevale la regola,
applicata alla serie di sedute effettiva.

### S-DEV — campione di sviluppo e robustezza

Lo snapshot del dataset usato per l'analisi V1, identificato da SHA-256.
Serve a stimare i modelli (sezione 7) e per la Parte A. `max_release_at_V1` è
il valore massimo di `release_at` in questo snapshot.

### S-HOLD — holdout temporale (outcome maturati, non esaminati)

Eventi ammissibili con `release_at > max_release_at_V1`, `release_at` successivo
al 2026-09-18 (congelamento di V1) e riferimento ≤ 2026-10-20. I loro outcome
erano osservabili al momento del congelamento V2 ma, secondo quanto dichiarato,
non sono stati esaminati né usati nello sviluppo. Questa dichiarazione va
firmata nella PR prima dell'esecuzione (sezione 13). S-HOLD si riporta
separatamente e non ha valore confermativo.

### S-PROSP — campione prospettico rigoroso (confermativo)

Eventi ammissibili con riferimento compreso tra **2026-10-21 e 2026-12-11**
(finestra P1). Il loro D20 non era maturato a `T_freeze`, e modelli e regole
di previsione sono fissati prima che lo diventi. Con il calendario NYSE,
l'ultimo D20 di P1 matura il 2027-01-12.

**Dimensione minima:** N ≥ 150 eventi e almeno 6 settimane ISO distinte di
riferimento. Il numero di eventi utilizzabili **non è dato per acquisito**: si
determina applicando i criteri della sezione 4 e si riporta con il dettaglio
delle esclusioni.

**Estensione pre-specificata:** se P1 non raggiunge la dimensione minima, il
campione si estende ai riferimenti tra 2026-12-14 e 2027-03-12 (finestra P2).
In quel caso si esegue **una sola** analisi sul campione unito P1+P2, quando
tutti i D20 sono maturati. Non sono ammesse analisi intermedie con valore
inferenziale. Se nemmeno P1+P2 raggiunge la dimensione minima, il risultato è
`NOT ESTIMABLE`.

### Ripiego se S-DEV non è recuperabile

Se entro il **2026-12-11** lo snapshot V1 non viene recuperato e verificato con
il suo SHA-256, allora S-HOLD diventa il campione di sviluppo, la Parte B non
viene eseguita e la Parte C resta invariata. In questo caso, per S-HOLD vale
`release_at ≥ 2026-09-21`. La scelta è determinata solo dalla disponibilità
dello snapshot, non dai dati.

## 6. Covariate pre-evento (Pre-Event Covariates V0.1)

Si calcolano dalla stessa serie di barre giornaliere (stesso fornitore, stesse
rettifiche) usata dall'Outcome Engine, usando **solo** sedute fino a
`reference_session_date` inclusa. Per tutti gli eventi ammessi questa seduta
termina prima della pubblicazione del comunicato.

- **`sigma_pre_60`:** deviazione standard di popolazione dei 60 rendimenti
  logaritmici giornalieri di chiusura che terminano alla seduta di riferimento
  (servono 61 chiusure consecutive nella serie), annualizzata × √252 × 100.
  È la stessa formula di `realized_volatility` nell'Outcome Engine, applicata
  alla finestra precedente. Se mancano chiusure, o una chiusura è ≤ 0, il
  valore è mancante.
- **`dollar_volume_pre_60`:** media di (chiusura × volume) sulle stesse 60
  sedute che terminano alla seduta di riferimento. È una proxy di dimensione e
  liquidità disponibile al momento dell'evento, congelata qui come previsto
  dalla sezione "Sensitivity" di V1.
- **`log_word_ratio`:** ln(`current_word_count` / `previous_word_count`).
- **`log_word_count`:** ln(`current_word_count`).
- **`after_market`:** 1 se `event_timing == "AFTER_MARKET"`, 0 se
  `PRE_MARKET`.

Nessuna covariata viene imputata, troncata o trasformata in modo diverso da
quanto scritto qui.

## 7. Modelli (definiti e congelati prima dei risultati)

Outcome trasformato per la stima: `y = ln(1 + absolute_return_pct_20)`, con
`absolute_return_pct_20` espresso in punti percentuali.

### Modelli primari

```text
M0:  y = a0 + b0 · ln(sigma_pre_60)                    + e
M1:  y = a1 + b1 · ln(sigma_pre_60) + c1 · h2_novelty  + e
```

### Modelli secondari (con controlli aggiuntivi)

```text
M0x: y = a + b · ln(sigma_pre_60) + d1 · ln(dollar_volume_pre_60)
         + d2 · log_word_ratio + d3 · log_word_count + d4 · after_market + e
M1x: M0x + c · h2_novelty
```

Effetti fissi di trimestre e di settore **non** entrano nei modelli
predittivi: un trimestre futuro non ha un coefficiente stimabile e non esiste
una classificazione settoriale point-in-time verificata. Gli effetti fissi di
trimestre compaiono solo nella sensibilità della Parte A.

### Stima e regola di previsione

- Stima OLS su S-DEV, una sola volta, con tutte le righe ammissibili. Nessuna
  selezione di variabili, interazione o termine aggiuntivo.
- Coefficienti, versioni delle librerie e SHA dei dati di stima vengono
  salvati in un file di modello congelato (con hash) **prima** di caricare gli
  outcome di S-HOLD o S-PROSP.
- Previsione per un nuovo evento: `pred = exp(ŷ) − 1`, in punti percentuali.
  Sotto il modello logaritmico è una previsione di tipo mediana, coerente con
  la funzione di perdita assoluta usata per la valutazione.
- Per l'outcome secondario chiave `abs(excess_return_pct_20)` si stimano su
  S-DEV, con le stesse forme funzionali, i modelli `M0e` e `M1e`.

## 8. Analisi

### Parte A — Robustezza su S-DEV (non confermativa)

1. Stima di M1 e M1x. Si riporta `c1` (e `c` per M1x) con errore standard CR1
   raggruppato per ticker e un p-value unilaterale (c > 0) da wild cluster
   bootstrap-t con imposizione dell'ipotesi nulla. Parametri: cluster =
   ticker, pesi di Rademacher, 10.000 repliche, seed `20261118`, stesse
   regole di validità delle repliche di V1.
2. Sensibilità: M1 con effetti fissi di trimestre di calendario della seduta
   di riferimento.
3. Sottoperiodi per anno di calendario di `release_at`: segno e dimensione
   di `c1` per ciascun anno.
4. Miglioramento in campione: variazione di R² e di MAE tra M0 e M1. Solo
   descrittivo.

### Parte B — Holdout temporale S-HOLD (non confermativa)

I modelli congelati si applicano senza nuova stima. Si riportano le stesse
metriche della Parte C, con la dicitura "holdout temporale, outcome maturati
prima del congelamento V2".

### Parte C — Validazione prospettica S-PROSP (confermativa)

**Metrica primaria:** riduzione relativa dell'errore assoluto medio

```text
ΔMAE_rel = 1 − MAE(M1) / MAE(M0)
MAE(Mk) = media_i | absolute_return_pct_20_i − pred_k,i |
```

**Test primario:** differenziale di perdita per evento
`d_i = |r_i − pred_0,i| − |r_i − pred_1,i|`. H0: E[d] ≤ 0; H1: E[d] > 0.

- Statistica: `t = media(d) / SE`, con SE CR1 raggruppato per **settimana ISO
  della seduta di riferimento**. La dipendenza principale tra eventi
  prospettici è il periodo comune: finestre a 20 sedute sovrapposte e shock
  di mercato condivisi. Nella finestra P1 ogni ticker compare di norma una
  sola volta.
- p-value unilaterale da wild cluster bootstrap-t con ipotesi nulla imposta
  (modello ristretto con media zero, quindi `d*_i = w_g · d_i`, dove `w_g` è
  il peso del cluster dell'evento; `t*` ricalcolato con lo stesso SE CR1):
  cluster = settimana ISO,
  **pesi a sei punti di Webb** (adatti a pochi cluster), 9.999 repliche, seed
  `20261118`. p = (1 + #{t* ≥ t}) / (1 + B_validi). Le repliche non valide si
  contano e si riportano; con meno del 95% di repliche valide il test è
  `NOT ESTIMABLE`.
- Sensibilità (non sostitutiva): stesso test con cluster = ticker, e con
  clustering a due vie ticker × settimana se il campione P1+P2 contiene
  ticker ripetuti.

**Metriche secondarie** (riportate tutte, senza selezione):

- ΔMAE_rel per M0x contro M1x;
- correlazione di rango (Spearman) tra previsione e outcome realizzato per M0
  e M1, e la loro differenza;
- ΔMAE_rel per `M0e` contro `M1e` su `abs(excess_return_pct_20)` (secondario
  chiave);
- ΔMAE_rel separatamente per `PRE_MARKET` e `AFTER_MARKET` (descrittivo);
- elenco degli eventi con |d_i| maggiore (primi 10), per audit.

## 9. Criteri di successo e di fallimento

**Soglia di interesse pratico:** ΔMAE_rel ≥ **3,0%**. Riduzioni inferiori sono
considerate economicamente irrilevanti per un previsore di ampiezza, anche se
statisticamente significative.

H2A-V2 è classificata **SUPPORTED-V2** solo se nella Parte C valgono **tutte**
le condizioni:

1. ΔMAE_rel ≥ 3,0%;
2. p-value primario unilaterale < 0,05;
3. `c1` stimato su S-DEV > 0;
4. ΔMAE_rel del secondario chiave (`M0e` contro `M1e`) > 0, anche se non
   significativo.

Altrimenti è classificata **NOT CONFIRMED-V2**. Si classifica
**NOT ESTIMABLE-V2**, che equivale a non confermato, se la dimensione minima
non è raggiunta neanche con P1+P2, se il test non è stimabile o se i modelli
non sono stimabili su S-DEV.

**Conseguenza pre-registrata:** con NOT CONFIRMED-V2 o NOT ESTIMABLE-V2, H2A
viene archiviata nel registro degli esperimenti e non si avviano estensioni
(direzione a lungo orizzonte, opzioni) basate su H2A.

H2A-V2 non è confermata se il risultato:

- appare solo nei modelli secondari, in sottogruppi o in S-HOLD;
- dipende dalla rimozione di osservazioni estreme;
- dipende da una metrica, un orizzonte o una trasformazione diversi da quelli
  qui fissati.

## 10. Dati mancanti, outlier e riproducibilità

- Valgono tutte le regole di V1 su dati mancanti, outcome pendenti, outlier e
  riproducibilità.
- Nessuna imputazione di H2, covariate, outcome o benchmark.
- Gli outlier restano nell'analisi primaria. Una sensibilità winsorizzata al
  1°/99° percentile dell'outcome, mai di H2 o delle covariate, può essere
  riportata come tale, ma non può salvare un test fallito.
- All'esecuzione si registrano: SHA del commit del codice, SHA-256 di ogni
  snapshot (S-DEV, S-HOLD, S-PROSP, prezzi, universo), SHA-256 del file di
  modello congelato, versioni di Python e librerie, data e ora di ogni fase.
- Si conservano gli identificativi e i conteggi intermedi di ogni inclusione
  ed esclusione, per ciascun campione.

## 11. Ordine di esecuzione obbligatorio

1. Implementare e testare Pre-Event Covariates V0.1 su dati sintetici;
   congelarne il commit.
2. Recuperare e verificare lo snapshot S-DEV, oppure applicare il ripiego
   della sezione 5.
3. Calcolare le covariate su S-DEV, stimare i modelli e salvare il file di
   modello congelato con il suo hash.
4. Eseguire la Parte A.
5. Costruire S-HOLD ed eseguire la Parte B.
6. Dopo la maturazione dell'ultimo D20 di P1 (2027-01-12), costruire S-PROSP,
   applicare la regola di dimensione minima ed eseguire la Parte C una sola
   volta.

Nessun passo può modificare quanto congelato nei passi precedenti.

## 12. Controllo delle modifiche

Qualunque modifica sostanziale dopo `T_freeze` richiede un nuovo protocollo
(`H2_PROTOCOL_V2.1` o superiore) con un nuovo istante di congelamento. Sono
sostanziali le modifiche a: modelli, covariate, trasformazioni, metrica,
soglia, test, campioni, finestre o criteri di ammissibilità.

Con un nuovo congelamento, la regola di maturazione si ricalcola: gli eventi
maturati prima del nuovo congelamento escono dal campione prospettico.

Le correzioni tipografiche senza effetto operativo vanno comunque annotate. Nel
dubbio, si emette una nuova versione.

## 13. Punti aperti da chiudere prima dell'esecuzione (non modificano il protocollo)

- [ ] Recuperare lo snapshot del dataset V1 e registrarne SHA-256 e
  `max_release_at_V1`.
- [ ] Dichiarazione firmata del responsabile: dopo il congelamento V1 nessun
  outcome di eventi successivi al 2026-09-18 è stato esaminato in relazione a
  H2.
- [ ] Verificare che la serie di prezzi dell'Outcome Engine copra almeno 61
  sedute prima di ogni evento di S-DEV e S-PROSP.
- [ ] Confermare l'universo di emittenti e la fonte dei comunicati per gli
  eventi prospettici (acquisizione con la stessa pipeline di V1).
