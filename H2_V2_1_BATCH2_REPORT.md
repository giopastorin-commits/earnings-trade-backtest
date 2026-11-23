# H2 V2.1 — Lotto prospettico 2

Data di esecuzione: 2026-11-23 (Europe/Rome)

## Confini e codice

| Voce | Valore |
|---|---|
| Branch | `research/h2-v2-1-build-inputs` |
| Commit di parametrizzazione pubblicato prima dell'acquisizione | `1d49ee569750e7f2ddd6923b48ac84f2f7ed229e` |
| Messaggio del commit | `Parametrize batch execution, no algorithmic change` |
| Finestra `reference_session_date` | 2026-11-18–2026-11-20 |
| Ultima seduta XNYS completata al momento della fissazione del lotto | 2026-11-20 |
| Modello congelato SHA-256 | `16a74ac113444dc6cc4d2c27c4f01ad1dd1aa462232dc55303677f07a89db087` |
| Nuova stima del modello | No |
| Outcome PROSP caricati o calcolati | No |

La prova offline di neutralità del codice parametrizzato è documentata in `H2_V2_1_BATCH_PARAMETRIZATION_REPORT.md`: input e previsioni del lotto 1 sono stati riprodotti con hash identici prima di qualunque acquisizione per il lotto 2. La suite pertinente ha dato 111 test superati.

## Snapshot sorgente

| Fonte | Periodo acquisizione UTC | SHA-256 d'insieme | SHA-256 manifesto |
|---|---|---|---|
| SEC, cutoff fisso 2026-11-23 09:00:00 UTC | avvio 2026-11-23 09:00:00 UTC | `97fd552b221116c389a5c520c84ec0cefaa990b47a7005528c83ec9182c75434` | `e04a724767fc569aa0aa9099ed9cc87559b1b5a8fa044c86769e5c8c1f8c2796` |
| Twelve Data `time_series`, `adjust=none`, target 2026-11-20, richiesta fino al 2026-11-21 | 2026-11-23 09:32:01–10:47:23 UTC | `082a78e3d4c595cc1bd6e002f5eb3ec181e23a82d4a6cadbeca34cf16eef066f` | `faa8f4b5530a838e70bd464161d8508b3f319e93a11cda5adb9885faa74e5282` |
| Twelve Data `time_series`, `adjust=splits`, 96 ticker DEV/PROSP, stessi estremi | 2026-11-23 11:12:08–11:25:24 UTC | `e5700c50cefc25c2abb553c56a8900dfc8ca512a5ddf82a24f5d5a1b35d11bb9` | `d77fc91400df40b23f096047e1ed7cdd3b9da3c2fceb20cfaf1d1952b319293f` |

I due snapshot Twelve Data sono nuovi, separati dagli snapshot del lotto 1 e conservati localmente fuori dal repository. Non sono state mescolate fonti.

## Input e controllo split

| Conteggio | Valore |
|---|---:|
| Eventi candidati PROSP_BATCH2 | 6 |
| Ammissibili finali PROSP_BATCH2 | 6 |
| Esclusi `CORPORATE_ACTION_SPLIT` | 0 |
| Esclusi `SPLIT_CHECK_UNAVAILABLE` | 0 |
| Fattori di rettifica rilevati nell'universo controllato | 1 |
| Fattori che intersecano una finestra evento PROSP_BATCH2 | 0 |

L'input finale `h2_v2_1_inputs_prosp_batch2.csv` ha SHA-256 `589fd0486599d2de2a6aba37c71c8bcdc30ef6eae17d5a730b1b5aaf1fd8d91b`. La verifica split ha letto entrambe le serie soltanto fino alla seduta di riferimento pertinente, secondo il codice congelato.

La pipeline invariata produce anche una copia tecnica DEV nel proprio output di lavoro. Non è un nuovo snapshot DEV, non sostituisce il file congelato D-001 e non viene incluso tra gli artefatti di consegna del lotto 2.

## Previsioni e sigillo

| Voce | Valore |
|---|---|
| Eventi con previsioni | 6 |
| Eventi non previsti | 0 |
| SHA-256 `h2_v2_1_predictions_batch2.csv` | `9bd474912c79bc0cb0597778f1e69e40e1d9496eb2cf678d9d95ae47cdf8fbb8` |
| `S_seal` | 2026-11-20 |
| Eventi del lotto con D20 ≤ `S_seal` | 0 |
| SHA-256 `H2_V2_1_SEAL_MANIFEST.json` dopo il blocco `prosp_batch2` | `5d2f27d61d1c7a10296ce25d8ae38e9fa4df4b644b3d40c543ffa6723c6a132a` |

Il blocco `prosp_batch2` registra gli hash dell'input, delle previsioni, del modello, dei due snapshot e delle finestre prezzi troncate per i sei eventi. Non contiene outcome PROSP.

## Hash degli artefatti di provenienza

| Artefatto | SHA-256 |
|---|---|
| `config/h2_v2_1_batch2.json` | `c31e95bf612e99ef7cace6f174f702add89c3d9ac8142f6513491e280ce63f38` |
| `h2_v2_1_build_manifest.json` | `7ef774811ff77892c2d786d8a6474c6549cad6b6ad165029b11c81dcee3c2d49` |
| `h2_v2_1_price_source_manifest.json` | `faa8f4b5530a838e70bd464161d8508b3f319e93a11cda5adb9885faa74e5282` |
| `h2_v2_1_sec_text_manifest.json` | `517de8f4b2332910fe4e73f0e341054e34ae97e815b4bc1cdb0715d318a857da` |
| `h2_v2_1_share_class_resolution.json` | `8cecab5ac3753d6d5312807061f3aeb3eca6cffee1293e864899997497408e27` |
| `h2_v2_1_phase1_1_b_manifest.json` | `6bf49538b33b162c26d047c507e30f1c6c8462318d10dc4b1397a79ea248813d` |
| `h2_v2_1_exclusions.csv` | `bb142eeb48dd5c43865021d732408cc009d7f3311ac6959ab4c4af59a0bfba90` |
| `h2_v2_1_split_adjusted_source_manifest.json` | `d77fc91400df40b23f096047e1ed7cdd3b9da3c2fceb20cfaf1d1952b319293f` |
| `h2_v2_1_split_coverage_audit.json` | `13a60c0bf91e519b43518c33aa063de25a7ae3ea8c0fa4e4b32d7cecbc961603` |
| `h2_v2_1_detected_split_factors.csv` | `7852ef25667707dfa8827ec098519289fc806023792369ffeb4802b9bfaabb15` |

## Deviazioni

Nessuna deviazione algoritmica, nessuna nuova fonte, nessuna ristima e nessun outcome prospettico. Il nuovo codice parametrizzato è esattamente quello del commit pubblicato prima delle acquisizioni.
