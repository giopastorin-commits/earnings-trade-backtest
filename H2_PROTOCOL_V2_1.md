# TRINITY/H2 — Protocollo statistico V2.1

## 1. Identità e rapporto con le versioni precedenti

**Protocollo:** `H2_PROTOCOL_V2_1`

**Stato:** pre-registrato e congelato prima di qualunque analisi. Nessun
outcome di S-HOLD o S-PROSP e nessuna relazione tra H2, covariate e outcome è
stata calcolata o ispezionata per redigere questo documento.

**Versioni precedenti, invariate e conservate:**

- `H2_PROTOCOL_V1.md`: SHA-256
  `219c2f4243dfb7d16b9eeeaf55ca43de586be1a41cd898b675f6d7562f91f87d`.
- `H2_PROTOCOL_V2.md`: SHA-256
  `2ae5147cec9f38970834818d195ecbaf214e1cda918804d7830ea0344791f737`, commit
  `ba7d4ff943a77e1156686077434e85ac9dabba6c`.

**Struttura:** V2.1 è identico a V2 tranne le sezioni sostituite o aggiunte
qui sotto. Ogni riferimento a "V2" nelle parti invariate si legge come "V2.1".
Dove V2.1 e V2 differiscono, per l'esecuzione prevale V2.1. V2 resta un
documento distinto e non viene modificato.

**Modifiche rispetto a V2:**

1. Regola di sigillo e sensibilità sugli eventi sigillati in tempo
   (nuove sezioni 5-bis e 8-bis; sezioni 9 e 11 sostituite).
2. Requisito di liquidità rimosso dai criteri primari (sezione 4
   sostituita).
3. Clausola interpretativa sulla numerosità dei cluster (nuova sezione
   9-bis) e snapshot SHA-256 degli input H2 prima del calcolo degli outcome
   (sezione 11).
4. Definizione del confine di maturazione per sedute completate (sezione 5,
   paragrafo sostituito).
5. Testo della dichiarazione e nota sui timestamp (sezione 13 sostituita).

## 4. Criteri di ammissibilità (sostituisce la sezione 4 di V2)

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

Criterio aggiuntivo per **tutti** i modelli:

- `sigma_pre_60` disponibile (sezione 6 di V2).

Criterio aggiuntivo **solo per i modelli secondari** M0x e M1x:

- `dollar_volume_pre_60` disponibile e strettamente positivo.

Le metriche secondarie M0x/M1x si calcolano sul sottoinsieme che soddisfa
anche questo criterio, riportandone l'N. L'assenza di `dollar_volume_pre_60`
non esclude un evento dal campione primario.

Nessun criterio usa il valore numerico di H2, delle covariate o degli outcome.
Le righe escluse restano nel dataset di audit con il motivo di esclusione.

**Universo:** invariato rispetto a V2.

## 5. Campioni — definizione del confine (sostituisce i paragrafi "Istante di congelamento" e "Regola di maturazione" di V2)

**Istante di congelamento V2.1 (`T_freeze`):** l'orario del commit che
introduce questo file nel repository GitHub, insieme al suo SHA.

**Ultima seduta completata (`S_last`):** l'ultima seduta NYSE la cui chiusura
regolare (16:00 ora di New York) è avvenuta prima di `T_freeze`. Non conta
l'orario di apertura. Una seduta in corso a `T_freeze` non è completata.

**Regola di maturazione:** un evento è maturato al congelamento se la sua
`future_session_date` per l'orizzonte 20 è ≤ `S_last`. Altrimenti è non
maturato. La regola usa solo prezzi di chiusura, come l'outcome primario.

Per un congelamento il 2026-11-18 prima della chiusura NYSE (22:00 ora
italiana), `S_last` è il 2026-11-17. I confini di S-HOLD
(riferimento ≤ 2026-10-20) e di S-PROSP (riferimento da 2026-10-21 a
2026-12-11, estensione P2 invariata) restano **identici a V2**. Le date sono
informative: prevale la regola, applicata alla serie di sedute effettiva.

Il resto della sezione 5 di V2 (definizioni di S-DEV, S-HOLD, S-PROSP,
dimensione minima, estensione P2, ripiego) è invariato.

## 5-bis. Regola di sigillo (nuova)

**Principio:** il rischio da controllare non è la scelta dei coefficienti, già
determinati dalla procedura, ma la possibilità di modificare dati,
implementazione o campione di sviluppo dopo aver conosciuto i risultati.

Per **outcome** di un evento si intende qualunque grandezza calcolata con
prezzi successivi alla sua `reference_session_date`: rendimenti, escursioni,
volatilità realizzata successiva, rendimenti in eccesso.

### Manifesto di sigillo

Prima che qualunque outcome di S-HOLD o S-PROSP venga calcolato, caricato
nell'ambiente di analisi o visualizzato, si committa nel repository un file
`H2_V2_1_SEAL_MANIFEST.json` con almeno:

1. SHA-256 dello snapshot S-DEV e `max_release_at_V1`, oppure la
   dichiarazione che si applica il ripiego della sezione 5 di V2;
2. SHA del commit del codice Pre-Event Covariates V0.1 e dei test che lo
   verificano;
3. SHA-256 del file di modello congelato (coefficienti di M0, M1, M0x, M1x,
   M0e, M1e, versioni delle librerie);
4. SHA-256 dello **snapshot degli input H2** (sotto) per ogni lotto di
   eventi;
5. SHA-256 del **file di previsioni** (pred_0, pred_1 e varianti per ogni
   evento) per ogni lotto;
6. SHA-256 della serie di prezzi usata per le covariate, troncata per ogni
   evento alla sua seduta di riferimento.

### Snapshot degli input H2 (senza outcome)

Per ogni evento candidato di S-HOLD e S-PROSP lo snapshot contiene:

- `current_release_id`, ticker, `release_at`, `acceptanceDateTime` EDGAR,
  `event_timing`, `timing_ambiguous`, `reference_resolution`,
  `reference_session_date`;
- `previous_release_id` usato per l'abbinamento e `pairing_quality`;
- SHA-256 del testo normalizzato corrente e precedente;
- `h2_similarity`, `h2_novelty`, `current_word_count`,
  `previous_word_count`;
- `sigma_pre_60`, `dollar_volume_pre_60`;
- versioni e SHA dei componenti congelati.

Lo snapshot **non** contiene prezzi successivi alla seduta di riferimento.

### Lotti

Gli eventi prospettici si accumulano nel tempo, quindi input e previsioni
possono essere sigillati in più lotti. Ogni lotto è un commit separato che
aggiunge al manifesto gli hash dei propri file. Il file di modello è unico,
viene congelato con il primo lotto e non cambia nei lotti successivi.

Per ogni evento, `S_seal(evento)` è l'ultima seduta NYSE completata prima del
commit del lotto che contiene la sua previsione.

### Violazioni

Un evento il cui outcome è stato calcolato o visualizzato prima del commit
della sua previsione è registrato come violazione e resta nel campione
primario con un'etichetta. Se le violazioni superano il 5% di S-PROSP, il
risultato primario è riportato come **COMPROMESSO** e non può essere
classificato SUPPORTED.

## 8-bis. Sensibilità sugli eventi sigillati in tempo (nuova)

**S-SEAL:** sottoinsieme di S-PROSP formato dagli eventi con
`future_session_date` (orizzonte 20) > `S_seal(evento)`, cioè quelli il cui
D20 è maturato *dopo* che la loro previsione era già sigillata.

Su S-SEAL si ripete l'analisi primaria della Parte C (stessa metrica, stesso
test, stesso seed) e si calcola `ΔMAE_rel_seal`.

### Classificazione della sensibilità, fissata prima dei risultati

1. **NON INFORMATIVA:** N(S-SEAL) < 75, oppure meno di 4 settimane ISO
   distinte, oppure meno del 95% di repliche bootstrap valide.
2. Se informativa:
   - **COERENTE:** `ΔMAE_rel_seal` ≥ 1,5% (metà della soglia pratica);
   - **CONTRARIA:** `ΔMAE_rel_seal` < 0 **e** p-value unilaterale per
     E[d] < 0 inferiore a 0,10 (stesso wild cluster bootstrap-t, direzione
     opposta);
   - **INCONCLUDENTE:** tutti gli altri casi, cioè 0 ≤ `ΔMAE_rel_seal` <
     1,5%, oppure `ΔMAE_rel_seal` < 0 senza la significatività richiesta per
     CONTRARIA.

Un risultato inconcludente o non informativo **non** equivale a un risultato
contrario.

## 9. Criteri di successo e di fallimento (sostituisce la sezione 9 di V2)

**Soglia di interesse pratico:** ΔMAE_rel ≥ 3,0% (invariata).

**Criteri primari:** nella Parte C su S-PROSP valgono tutte le condizioni
1–4 della sezione 9 di V2, e il risultato non è COMPROMESSO.

**Classificazione finale:**

| Criteri primari | Sensibilità S-SEAL | Classificazione |
| --- | --- | --- |
| soddisfatti | COERENTE | **SUPPORTED-ROBUST-V2.1** |
| soddisfatti | INCONCLUDENTE o NON INFORMATIVA | **SUPPORTED-LIMITED-V2.1** |
| soddisfatti | CONTRARIA | **CONFLICTING-V2.1** |
| non soddisfatti | qualunque | **NOT CONFIRMED-V2.1** |
| non stimabili | qualunque | **NOT ESTIMABLE-V2.1** |

**Conseguenze pre-registrate:**

- **SUPPORTED-ROBUST:** H2A può proseguire verso il test successivo,
  pre-registrato con un nuovo protocollo.
- **SUPPORTED-LIMITED:** H2A non è dichiarata confermata in modo robusto. Si
  può proseguire solo con una nuova raccolta prospettica interamente sigillata
  in tempo, senza estensioni operative.
- **CONFLICTING, NOT CONFIRMED, NOT ESTIMABLE:** H2A viene archiviata nel
  registro e non si avviano estensioni basate su H2A.

Restano validi gli elenchi di V2 sui casi che non costituiscono conferma:
solo modelli secondari, sottogruppi, S-HOLD, rimozione di estremi, metriche o
trasformazioni diverse.

## 9-bis. Interpretazione e numerosità dei cluster (nuova)

Gli eventi di S-PROSP sono concentrati in poche settimane: P1 copre circa 8
settimane ISO. 150 eventi in 8 settimane non equivalgono a 150 osservazioni
indipendenti, perché condividono gli shock di mercato e le finestre
sovrapposte. I pesi di Webb correggono l'inferenza per il numero ridotto di
cluster, ma non creano informazione indipendente.

Di conseguenza:

- **NOT CONFIRMED-V2.1** è una decisione di allocazione delle risorse, non la
  dimostrazione che l'effetto sia nullo;
- un risultato positivo su S-PROSP vale per il periodo e il regime di mercato
  osservati e va descritto così;
- nessuna classificazione autorizza uso operativo o di trading (sezione 1 di
  V2, invariata).

## 11. Ordine di esecuzione obbligatorio (sostituisce la sezione 11 di V2)

1. Implementare e testare Pre-Event Covariates V0.1 su dati sintetici;
   congelarne il commit.
2. Recuperare e verificare lo snapshot S-DEV, oppure applicare il ripiego
   della sezione 5 di V2.
3. Calcolare le covariate su S-DEV, stimare i modelli e salvare il file di
   modello congelato.
4. Costruire lo snapshot degli input H2 e le previsioni per gli eventi
   S-HOLD e S-PROSP disponibili, senza alcun outcome. Committare il
   manifesto di sigillo (sezione 5-bis), primo lotto.
5. Per gli eventi prospettici successivi: sigillare input e previsioni in
   lotti, ciascuno prima della maturazione del D20 degli eventi che contiene,
   quando possibile.
6. Solo dopo il commit del primo lotto: eseguire la Parte A su S-DEV e la
   Parte B su S-HOLD.
7. Dopo la maturazione dell'ultimo D20 della finestra prospettica: calcolare
   gli outcome di S-PROSP, applicare la regola di dimensione minima ed
   eseguire una sola volta la Parte C e la sensibilità 8-bis.

La Parte A segue il sigillo perché il codice delle covariate e il file di
modello devono essere congelati prima di qualunque risultato di robustezza.
Nessun passo può modificare quanto congelato nei passi precedenti.

## 13. Punti aperti e dichiarazione (sostituisce la sezione 13 di V2)

- [ ] Recuperare lo snapshot del dataset V1 e registrarne SHA-256 e
  `max_release_at_V1`.
- [ ] Verificare che la serie di prezzi copra almeno 61 sedute prima di ogni
  evento di S-DEV, S-HOLD e S-PROSP.
- [ ] Confermare la fonte dei comunicati per gli eventi prospettici:
  acquisizione con la stessa pipeline di V1.
- [ ] Firmare la seguente dichiarazione nella PR prima del commit del primo
  lotto di sigillo:

> Dichiaro che, dopo il congelamento di `H2_PROTOCOL_V1` (2026-09-18), nessun
> outcome di eventi con `release_at` successivo al 2026-09-18 è stato
> calcolato, visualizzato o analizzato in relazione a H2 o alle covariate di
> questo protocollo. Questa dichiarazione non riguarda la conoscenza
> generale dei prezzi di mercato pubblici, che non è controllabile.

### Nota sui timestamp

`acceptanceDateTime` certifica l'accettazione del filing su EDGAR, non
necessariamente il momento della prima diffusione pubblica dei risultati, che
può avvenire prima tramite comunicato o agenzia. V2.1 **non** modifica la
classificazione temporale degli eventi: restano in vigore le regole
dell'Outcome Engine V0.2 e i criteri `timing_ambiguous == false` e
`reference_resolution != "AMBIGUOUS"`. Il valore di `acceptanceDateTime` viene
registrato nello snapshot degli input per audit, non per ridefinire la
seduta di riferimento.
