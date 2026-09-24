Elevation bounding box (first attempt) was too small — missed wards already visible in loaded ward data, and excluded Khadakwasla Dam entirely (dam sits ~21km from city center, outside the ward-derived extent). Fixed by deriving the true extent from `wards.total_bounds()` and manually padding westward to include the dam.


Elevation raster (SRTM GL1, 30m resolution) validated: CRS matches ward boundaries (EPSG:4326), no nodata values present, elevation range 516–1199m is plausible for Pune, and visually confirmed against known geography (drainage channels, Sinhagad/Western Ghats foothills visible in the correct location).


Ward boundary polygons have two small unexplained gaps near the city center — areas with no ward covering them. Cause unknown, noted as a known limitation of the source data.


Per-ward elevation computed via zonal statistics, validated against real geography: lowest-elevation ward is "Shivajinagar Gaothan - Sangamwadi" (Sangam = river confluence in the name itself), highest is "Katraj - Gokulnagar" (a known hilly area) — both consistent with actual Pune geography, not just plausible numbers.


POC flood localities (Ekta Nagar, Bhimnagar) could not be reliably matched to wards by name/text search — AI-summarized web searches gave two different, contradictory ward locations for the same locality name, sourced from low-authority sites (tender listings, Facebook posts, business directories). Common Indian locality names repeat across multiple areas of the same city, making name-based matching unreliable on its own.


Resolved locality-to-ward mapping using real coordinates plus a geopandas point-in-polygon check instead of trusting text search. Final confirmed mapping: Ekta Nagar → Nanded City - Sun City (ward 52); Bhimnagar → Warje - Kondhave Dhavde (ward 34); Shivne → Ramnagar - Uttamnagar Shivane (ward 35); Kondhwe Dhawade → Warje - Kondhave Dhavde (ward 34, same as Bhimnagar — confirmed genuine neighbors).


All 6 POC dam-discharge incidents cluster in only 3 adjacent wards (34, 35, 52) along the river corridor — geographically consistent with the river-corridor flood hypothesis, not scattered randomly.


Rainfall features (mean/max daily rainfall, heavy-rain-day %) computed from Shivajinagar station data as ward-level summary statistics — same value applied to every ward, not a true daily time series. Appropriate for a baseline model at this stage, but a known limitation: only one rain gauge exists for the whole city, so wards can't yet be distinguished by rainfall differences. True daily/live rainfall modeling deferred to the live risk-scoring service later in the project.


Incident count (renamed `dam_discharge_incident_count`) reflects only the 6 POC dam-discharge events, not rainfall-driven waterlogging — no systematic historical dataset exists yet for rainfall-caused incidents, so blending the two would overstate what the data actually supports.


Saved combined ward feature table (elevation, dam-discharge incidents, rainfall summary stats) as `data/processed/ward_features_v1.csv`.

Ward feature scaling: chose min-max normalization to bring elevation (516–1199 range) and incident count (0–2 range) onto a comparable 0–1 scale before combining — raw values would have let elevation dominate purely due to larger numbers, not actual importance.

Elevation risk direction deliberately flipped in the normalization formula so low elevation produces high risk (not high risk), verified by checking both edge cases (min value → 1, max value → 0) before trusting it.

Risk score weights: elevation 0.4, incident count 0.6. Reasoning: incidents are real ground-truth evidence (confirmed past flooding), elevation is a weaker proxy signal — so ground truth weighted higher. Not the only valid choice, a deliberate v1 call.

Baseline ward-level risk model validated: the 3 wards known to have flooded in the POC (Warje-Kondhave Dhavde, Nanded City-Sun City, Ramnagar-Uttamnagar Shivane) came out as the top 3 highest risk scores out of all 58 wards, clearly separated from the rest (0.89, 0.68, 0.67 vs. ~0.40 and below). This is the M1 backtest — model correctly re-identified known real flood locations using only elevation and incident data.

Known limitation: wards ranked 4th and below are nearly tied (within ~0.006 of each other) — elevation alone doesn't meaningfully differentiate wards once outside the top 3, since rainfall features are currently uniform across all wards. Expected to improve once the river-corridor rule (M2) and real per-ward/live rainfall are added later.

Tested NASA GPM IMERG satellite rainfall (via Google Earth Engine) as a fix for the single-rain-gauge limitation. Confirmed data completeness (48/48 half-hourly images per day) and ruled out point-vs-area averaging as the cause of discrepancy (7.03mm at exact point vs. 8.45mm over 5km radius — minimal difference). Found consistent, direction-stable underestimation vs. Shivajinagar ground truth on two test dates (2026-08-01: 1.99mm vs. 30.3mm; 2021-10-10: 7.03mm vs. 55.3mm) — a known, documented limitation of satellite rainfall products for intense South Asian monsoon convective rainfall. Concluded GPM/IMERG is not reliable for absolute rainfall values in this project; not pursued further for M1. Possible future direction (not pursued): using GPM only for relative spatial patterns across grid cells rather than absolute totals.

Final sweep for spatially-distributed rainfall data (beyond single-station Shivajinagar): tested IMD gridded climatology (wrong shape — 30yr normals, not real dates), IMD free data series (wrong shape — huge multi-state zones), NASA GPM IMERG satellite (real dates + real grid, but 7-15x underestimation on test dates), Open-Meteo/ERA5 reanalysis (same, ~5.5x underestimation). All three quantitative sources tested show consistent underestimation of intense monsoon rainfall — a structural limitation of currently free/accessible gridded rainfall products, not a fixable bug. Checked PMC's open data portal for waterlogging complaint records as an alternative ground-truth source — confirmed not publicly available. Conclusion: single-station Shivajinagar rainfall remains the primary/only reliable rainfall source for M1; documented as a known limitation with a real, evidenced search trail, not an oversight.

Computed river_distance_m for all 58 wards using OSM river network (Overpass Turbo export) and UTM 43N reprojection for accurate metric distance. Validated: all 3 known flood wards (34, 35, 52) correctly show 0.0 distance (directly touching the river). Limitation: 13 of 58 wards show exactly 0.0 — the feature distinguishes "touches river" vs. "doesn't" well, but can't differentiate proximity among the 13 river-adjacent wards themselves.

Corridor-distance recomputed using a bounding box isolating the Khadakwasla-to-city stretch specifically, replacing the citywide river network. Correctly captures all 3 known flood wards in the zero-distance group (an improvement over the first attempt, which missed them). Limitation unchanged: 13 wards still tied at exactly 0.0 — distance-to-nearest-river-line is inherently binary (touching vs. not), and doesn't differentiate degree of river exposure among touching wards. A length-of-riverfront-per-ward measurement would likely resolve this, noted as a future improvement, not pursued in M1.

Risk score updated to 3-way blend (elevation 0.3, incidents 0.5, corridor-distance 0.2). Result: known flood wards (34, 35, 52) now score 0.91/0.73/0.72, clearly separated from all other wards (next-highest: 0.50) — the cleanest separation achieved so far, an improvement over the 2-feature version. Middle-tier flat-tie limitation persists among river-touching wards (rows 4-11, ~0.47-0.50), consistent with the already-diagnosed binary nature of distance-to-river. Saved as ward_risk_v3.csv.

PostgreSQL + PostGIS + TimescaleDB set up via Docker (timescale/timescaledb-ha:pg16). Verified both extensions active: PostGIS 3.6, TimescaleDB 2.30.0.

Loaded all three tables into PostgreSQL: wards (58 rows, with geometry via to_postgis), rainfall_daily (2,330 rows), dam_discharge_events (7 rows — 6 POC events, one split across two localities). Hit and resolved duplicate-row issues from repeated notebook cell execution; fixed by explicitly truncating and verifying empty before reload, not just re-running append blindly. Exact dates for two Jul 2026 events not available in original sources (reported only as "Late Jul 2026") — approximated as 2026-07-25, flagged as imprecise.

Corridor threshold rule (25,000 cusecs) correctly flags wards 52 and 35, both directly named in dated, sourced flood events. Ward 34 (Bhimnagar/Kondhwe Dhawade) does not appear, since no specific dated event in the sourced POC table names it explicitly — it was only present in the general locality list, not tied to a confirmed dated event. Not treated as a bug: the corridor rule is intentionally scoped to directly-evidenced wards only. Ward 34 remains represented in the overall risk_score via elevation and river-distance features.

Built minimal FastAPI service exposing two endpoints (GET /risk/city, GET /risk/wards), reading live from PostgreSQL. Verified working via interactive docs (/docs) — correctly returns city-wide composite score and ranked ward scores, with corridor-discharge override triggering correctly at threshold.