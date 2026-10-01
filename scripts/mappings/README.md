# Reviewed parameter mappings

One file per company, read by `scripts/deliver_production.sh` step 3. Each line maps one official cell
to a comparison key: `product  table  row  cell  field  unit  condition  target  expect` (tab separated,
1-based indices, unit `-` for none). The raw cell text is never changed; `unit` says what the text is
written in and `condition` names the model/variant and any qualifier from the page. `expect` is the cell's
original text (whitespace collapsed) when it was reviewed: if a later run reads anything else in that cell
(the page changed, or an older data root parsed it differently), the run stops before packaging. This
caught an M5 data root whose earlier NVIDIA H200 parse had its rows two places apart.

## Rules used

- Map the value cell, never a label or model-name cell. Skip blanks, N/A, marketing text, certification lists.
- `field` is `<part>.<parameter>`: the target's part id with `-` → `_`, then a shared parameter name
  (power_rating, input_voltage, efficiency, cooling_capacity, height/width/depth, weight, …).
- A product bound to both `X.spec` and `X.operation` puts ratings, dimensions and configuration on `.spec`,
  and efficiency, power consumption and operating conditions on `.operation`.
- Multi-model tables: one representative row per product (the largest, or the one in the title), named in condition.
- Metric when the page gives separate metric and imperial rows.
- Every bound target gets at least one line; 3–6 key parameters each.

Each file was checked by mapping every line, packaging, and `scripts/verify_package.py` reporting
`complete: true` (2026-10-01, all seven companies, fresh collection).

## Vendor page problems found while mapping (recorded in the condition column)

- Vertiv EnergyCore Li5/Li7: the energy range (kWh) is printed under "Output Voltage Range"; mapped as `energy_capacity`.
- Vertiv CoolChip CDU 600/70 and FF3175: capacity header says "kBtuh [kW]" but the value is kW (FF3175 also tons).
- Vertiv racks: VR3307 depth 43.3 in vs 47.8 in for the rest of the 33xx line; several weights blank; not mapped.
- Supermicro SRS-GB300-NVL72: "8x 1U 33kW … total power 132kW" (8 × 33 = 264); rack dimensions printed without unit (read as mm).
- Siemens NXAIR: depth "1500 mm 4)" does not apply to the 4000 A panel used for the other dimensions (footnote: 1540 mm); depth not mapped.
- Delta: PSU depth 640 mm on the PSU page vs 520 mm on the 33 kW system page; efficiency ">97.5" without %.

## Judgment calls to revisit

- Vertiv EnergyCore Li7 is bound to `P.bbu.operation`; `P.ups-battery` may fit better.
- NVIDIA DGX B200 is bound only to `P.gpu.operation`, so its configuration figures sit there next to the system power.
- Short numeric values from PDF catalogs (e.g. Siemens "40", "4000") are confirmed only as present in the PDF text, not tied to their row by the verifier; the cell choice itself was checked against `pdftotext -layout`.
