# Analisi di robustezza, non confermativa (N piccolo)

Solo S-DEV. Nessun outcome PROSP è stato caricato o calcolato.

## Inferenza CR1 e wild cluster bootstrap-t

| Modello | N | Cluster ticker | coefficiente H2 | SE CR1 | t | p unilaterale | repliche valide |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| M1 | 81 | 81 | 0.54405895896020484 | 0.42610388936907789 | 1.2768223255736535 | 0.1334866513348665 | 10000 / 10000 |
| M1x | 81 | 81 | 1.7456837838148034 | 0.96882181536410128 | 1.8018625882807386 | 0.021097890210978902 | 10000 / 10000 |

## Sensibilità con effetti fissi di trimestre

N=81; coefficiente H2=0.69049872163967618; SE CR1=0.4154231873400373; livelli=2026-Q3, 2026-Q4.

## Sottoperiodi per anno di release_at

| Anno | N | coefficiente H2 |
| --- | ---: | ---: |
| 2026 | 81 | 0.5440589589602048 |

## Miglioramento descrittivo in campione

R² è calcolato sulla risposta trasformata; MAE è in punti percentuali sulla scala originale di absolute_return_pct_20.

| Metrica | M0 | M1 | Δ M1−M0 |
| --- | ---: | ---: | ---: |
| R² | 0.052499009376852146 | 0.05996963748612627 | 0.0074706281092741245 |
| MAE | 4.5254933485867026 | 4.4481339719055581 | -0.077359376681144454 |

Bootstrap: Rademacher, ipotesi nulla imposta, 10000 repliche, seed 20261118.
