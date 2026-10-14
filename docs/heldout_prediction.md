# Held-out prediction (preregistered 2026-10-14, before any EMSR927 reference is opened)
Based only on `eval/results/*.csv` (latest full-split rows) and `docs/open_items.md`.
- **Flood F1**: dev mean strict 0.426, tolerant 0.126-0.923 over 6 events (worst
  bosnia_konjic 0.038, best pakistan_larkana 0.815); gate needs 0.90 mean/0.80 floor.
  Expect Trishuli strict ~0.40-0.45, tolerant ~0.55-0.65 (glacial valley like Norway
  events, debris/turbidity closer to low scorers). **Gate: FAIL expected.**
- **Damage F1**: dev means road 0.400, building 0.181 over 6 events, building_f1 = 0.0
  on two (bhutan, pakistan). Gate needs 0.90 both. Expect road ~0.35-0.55, building
  ~0.10-0.25. **Gate: FAIL expected.**
- **Cut-off F1**: dev mean 0.368, 3 of 6 events at 0.0, only Norway/Pakistan score well
  (0.90, 0.95); metric definition changed between runs (open_items item G). Expect
  ~0.30-0.50. **Gate: FAIL expected.**
None of these held-out numbers are measured; each is a range inferred from dev-event
gaps already logged in `docs/open_items.md`.
