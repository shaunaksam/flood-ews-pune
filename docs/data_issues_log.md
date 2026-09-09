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