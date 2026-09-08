Elevation bounding box (first attempt) was too small — missed wards already visible in loaded ward data, and excluded Khadakwasla Dam entirely (dam sits ~21km from city center, outside the ward-derived extent). Fixed by deriving the true extent from `wards.total_bounds()` and manually padding westward to include the dam.

---

Elevation raster (SRTM GL1, 30m resolution) validated: CRS matches ward boundaries (EPSG:4326), no nodata values present, elevation range 516–1199m is plausible for Pune, and visually confirmed against known geography (drainage channels, Sinhagad/Western Ghats foothills visible in the correct location).

---

Ward boundary polygons have two small unexplained gaps near the city center — areas with no ward covering them. Cause unknown, noted as a known limitation of the source data.

---

Per-ward elevation computed via zonal statistics, validated against real geography: lowest-elevation ward is "Shivajinagar Gaothan - Sangamwadi" (Sangam = river confluence in the name itself), highest is "Katraj - Gokulnagar" (a known hilly area) — both consistent with actual Pune geography, not just plausible numbers.

---

POC flood localities (Ekta Nagar, Bhimnagar) could not be reliably matched to wards by name/text search — AI-summarized web searches gave two different, contradictory ward locations for the same locality name, sourced from low-authority sites (tender listings, Facebook posts, business directories). Common Indian locality names repeat across multiple areas of the same city, making name-based matching unreliable on its own.

---

Resolved locality-to-ward mapping using real coordinates plus a geopandas point-in-polygon check instead of trusting text search. Final confirmed mapping: Ekta Nagar → Nanded City - Sun City (ward 52); Bhimnagar → Warje - Kondhave Dhavde (ward 34); Shivne → Ramnagar - Uttamnagar Shivane (ward 35); Kondhwe Dhawade → Warje - Kondhave Dhavde (ward 34, same as Bhimnagar — confirmed genuine neighbors).

---

All 6 POC dam-discharge incidents cluster in only 3 adjacent wards (34, 35, 52) along the river corridor — geographically consistent with the river-corridor flood hypothesis, not scattered randomly.

---

Rainfall features (mean/max daily rainfall, heavy-rain-day %) computed from Shivajinagar station data as ward-level summary statistics — same value applied to every ward, not a true daily time series. Appropriate for a baseline model at this stage, but a known limitation: only one rain gauge exists for the whole city, so wards can't yet be distinguished by rainfall differences. True daily/live rainfall modeling deferred to the live risk-scoring service later in the project.

---

Incident count (renamed `dam_discharge_incident_count`) reflects only the 6 POC dam-discharge events, not rainfall-driven waterlogging — no systematic historical dataset exists yet for rainfall-caused incidents, so blending the two would overstate what the data actually supports.

---

Saved combined ward feature table (elevation, dam-discharge incidents, rainfall summary stats) as `data/processed/ward_features_v1.csv`.