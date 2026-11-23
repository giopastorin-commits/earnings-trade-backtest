# H2 V2.1 — Report di parametrizzazione dei lotti

Data del controllo: 2026-11-23 (Europe/Rome)

## Ambito e neutralità algoritmica

La modifica sostituisce esclusivamente le costanti legate al lotto con un file di configurazione JSON obbligatorio da riga di comando. Non modifica algoritmi, filtri, formule, ordine delle operazioni o formati degli output. Il caricamento delle previsioni verifica sempre l'hash completo del modello congelato `16a74ac113444dc6cc4d2c27c4f01ad1dd1aa462232dc55303677f07a89db087`.

La prova di neutralità è stata eseguita interamente offline sugli snapshot già acquisiti del lotto 1, prima di qualsiasi acquisizione per il lotto 2. Il codice parametrizzato ha rigenerato:

| Artefatto | SHA-256 atteso | SHA-256 ottenuto | Esito |
|---|---|---|---|
| `h2_v2_1_inputs_prosp_batch1.csv` | `2a01fb5d0cbf8b110779bd61d2f94116877dde15fbef1dd4a91d757d3e9a1493` | `2a01fb5d0cbf8b110779bd61d2f94116877dde15fbef1dd4a91d757d3e9a1493` | IDENTICO |
| `h2_v2_1_predictions_batch1.csv` | `5554daa42e1ffc09475badb044590017cc53763f1b31ba5f72f043166adf6534` | `5554daa42e1ffc09475badb044590017cc53763f1b31ba5f72f043166adf6534` | IDENTICO |

Il test automatico `test_batch1_parameterization_reproduces_frozen_hashes` ricostruisce le previsioni dal file di input e dal modello congelati, controlla l'hash dell'input e richiede l'identità byte-per-byte dell'hash delle previsioni. La suite pertinente ha dato `111 passed` il 2026-11-23.

## Costanti sostituite e valori storici del lotto 1

| Costante o valore prima della modifica | Parametro/configurazione | Valore lotto 1 |
|---|---|---|
| identificativo fisso `PROSP_BATCH1` | `batch_id` | `PROSP_BATCH1` |
| suffisso/nomi file fissi `batch1` | `batch_slug` | `batch1` |
| `PROSP_REFERENCE_START` | `prosp_reference_start` | `2026-10-21` |
| `PROSP_REFERENCE_END` | `prosp_reference_end` | `2026-11-17` |
| `PRICE_TARGET_SESSION` | `price_target_session` | `2026-11-17` |
| `PRICE_RUN_ID` | `price_run_id` | `H2V21_PHASE1_20261120_A2_PSKY` |
| split `TARGET_SESSION` | `price_target_session` | `2026-11-17` |
| split `REQUEST_END_DATE` | proprietà derivata `request_end_date` | `2026-11-18` |
| split `CANONICAL_TICKER_COUNT` | `expected_split_ticker_count` | `458` |
| split `PARENT_SNAPSHOT_COMMIT` | `parent_snapshot_commit` | `546ea38f7376adf9480a0da07048ed8ecc561ea3` |
| split `NONE_ENSEMBLE_SHA256` | `expected_none_ensemble_sha256` | `8dd59aae14c92f9d8dd9a6c055c166157e8b49b0d6ce0b48591080277ec7a54f` |
| hash split atteso | `expected_split_ensemble_sha256` | `44dc22079a57f4e60a07822c259503377bd80f72a04929bffc25db9a802c50d2` |
| Phase 2 `PROSP_SHA256` | `expected_input_sha256` | `2a01fb5d0cbf8b110779bd61d2f94116877dde15fbef1dd4a91d757d3e9a1493` |
| hash previsioni lotto 1 atteso | `expected_predictions_sha256` | `5554daa42e1ffc09475badb044590017cc53763f1b31ba5f72f043166adf6534` |
| hash modello congelato | `expected_model_sha256`, con vincolo interno invariabile | `16a74ac113444dc6cc4d2c27c4f01ad1dd1aa462232dc55303677f07a89db087` |
| nomi input/previsioni/manifest fissi del lotto 1 | proprietà derivate da `batch_slug` | nomi originali `...batch1.csv` |

Tutti questi valori sono registrati in `config/h2_v2_1_batch1.json`.

## Diff completo per file

I conteggi sono quelli di `git diff --numstat` prima del commit; il file di configurazione è nuovo. Gli intervalli indicano le righe interessate nel file risultante, ricavati dagli hunk del diff a contesto zero.

| File | Aggiunte | Rimozioni | Righe/hunk interessati nel file risultante |
|---|---:|---:|---|
| `scripts/h2_v2_1_build_inputs.py` | 18 | 5 | 21, 41, 60, 65, 69, 76, 94–96, 119, 121, 133–135, 141, 144, 147, 151 |
| `scripts/h2_v2_1_phase2.py` | 58 | 2 | 17, 23, 26, 38–55, 77–110, 116, 126, 136 |
| `tests/trinity/test_h2_v2_1_phase2.py` | 41 | 4 | 4, 14–15, 170–175, 178, 197–202, 204–206, 250–271 |
| `tests/trinity/test_twelvedata_split_factors.py` | 22 | 14 | 4, 21, 107, 115, 133, 139, 144, 152, 160, 221, 236, 240, 244, 255, 276, 280, 285, 289, 315, 369, 373, 379 |
| `trinity/h2_v2_1_inputs.py` | 121 | 25 | 109–192, 337–340, 346–347, 582, 590, 749–751, 760–761, 767, 771, 794, 800–801, 1164, 1185, 1192, 1194, 1282–1284, 1291, 1315, 1361, 1382, 1396, 1414, 1452–1455, 1574, 1580 |
| `trinity/h2_v2_1_phase2.py` | 123 | 11 | 22–23, 90, 94–96, 98–99, 101–103, 466, 470–476, 505, 510–531, 616–696 |
| `trinity/twelvedata_split_factors.py` | 67 | 49 | 16–17, 74–76, 79, 88, 94–95, 97, 103–105, 111, 116, 248, 256, 258–259, 262–264, 282, 289, 304, 309, 342, 363, 431–433, 447–450, 452, 464, 503–505, 507, 527–529, 531, 587, 591, 598, 604–605, 608–611, 616, 621, 761, 771, 781, 860, 887, 891, 897, 904, 908, 914–915 |
| `config/h2_v2_1_batch1.json` | 15 | 0 | 1–15 (nuovo) |

Il diff completo è contenuto nel commit separato con messaggio `Parametrize batch execution, no algorithmic change`.

## Sequenza di controllo

1. Esecuzione offline con la configurazione del lotto 1 e gli snapshot esistenti.
2. Verifica dei due SHA-256 congelati: entrambi identici.
3. Esecuzione dei test pertinenti: 111 superati.
4. Controllo `git diff --check`: nessun errore di whitespace.
5. Nessuna acquisizione o richiesta di rete per il lotto 2 prima del push del commit di parametrizzazione.
