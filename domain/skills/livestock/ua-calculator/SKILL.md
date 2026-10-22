---
name: ua-calculator
description: Animal Unit (AU) calculation - actual stocking rate and carrying capacity of a registered pasture, using the dry-matter production returned by the biomass tool.
license: GPL-3.0-or-later
metadata:
  version: "1.0.0"
  tags: ["animal unit", "stocking rate", "carrying capacity"]
---

# Animal Unit (AU) calculation

Use this skill to estimate how many animals a registered pasture can support (carrying capacity), to compute the actual stocking rate of a herd, or to convert a herd into Animal Units (AU). It is a simple, order-of-magnitude demonstration based on global satellite data; local policies, breeds, management and field data would refine it.

## When to Use

- The user asks for the stocking rate of an area, or whether the area supports the current herd.
- The user asks for the carrying capacity (ideal stocking) of a pasture.
- The user wants a herd converted to Animal Units.
- You must interpret dry-matter production returned by the biomass tools in terms of animals.

## Process

1. **Get the forage**: call `get_biomass_total` for the registered area. Note `mean_t_ha`, `total_t`, the productive pasture hectares, the period and its number of days, and the source (Global Pasture Watch annual or Time2Graze). Prefer the annual Global Pasture Watch year (the default of the tool). Use Time2Graze only when the user asks for recent data or no annual data exists.
2. **CRITICAL - check the period before any calculation**: the formulas below need an ANNUAL total of dry matter (t DM/year).
   - If the data is a full Global Pasture Watch year (365 or 366 days), use `total_t` as is.
   - If it is a Time2Graze period (a month or part of a year), annualize with the factor 365 / days covered (days are reported with the data): annual total = total_t x 365 / days covered. NEVER multiply a month by 12 and never skip this check. Tell the user explicitly that the carrying capacity extrapolates a short period to a year and may be far from the real annual production (growth is seasonal).
3. **Collect the herd** when the stocking rate is needed: number of animals and average live weight, so the total live weight (kg) is known. Ask for it if missing. If the user already gives the herd in AU, use it directly and skip the conversion.
4. **State the assumptions** (see below) and let the user override them.
5. **Calculate with the calculator tools**, step by step, one binary operation per call (multiply, divide...). Never compute in your head.
6. **Explain didactically**: show the formula with the numbers substituted, the period and source of the forage, whether it was annualized, and a short diagnosis (does the area support the herd?). Present results as an order of magnitude, not as a measurement.

## Formulas

- 1 AU = 450 kg of live weight.
- Total AU of the herd = total live weight (kg) / 450
- Actual stocking rate (AU/ha) = total AU / pasture area (ha), using the productive pasture hectares of the biomass result.
- Annual demand per AU = 450 kg x 2.5% per day x 365 days = 4.106 t DM/AU/year (about 4.1).
- Protection factor 2: only about 50% of the forage can be harvested; the rest is lost to trampling, leftovers and regrowth needs. Demand with protection = 4.106 x 2 = 8.2125, rounded to 8.2 t DM/AU/year (use 8.2 in the calculations and say it is rounded).
- Carrying capacity (AU) = annual forage (t DM/year) / 8.2
- Carrying capacity per hectare (AU/ha) = carrying capacity (AU) / pasture area (ha)
- Always divide by the productive pasture hectares of the biomass result (`pasture_ha`), not by the whole property or registered area.
- Diagnosis: compare the actual stocking rate with the carrying capacity per hectare.

## Assumptions to declare (and accept overrides)

- 1 AU = 450 kg live weight (a reference animal; other values can be used if the user gives them).
- Daily dry-matter intake of 2.5% of live weight.
- Protection factor 2 (50% of the forage harvested).
- The dry-matter figures come from a general productivity-to-dry-matter conversion calibrated for cultivated tropical pastures; elsewhere they are only an order of magnitude.

## Best Practices

- Always say which period and source the forage came from, and whether it was annualized.
- Never promise yields, performance or a safe stocking rate: the result is an estimate that does not replace field verification or local technical advice.
- Show the substituted numbers (for example: "we divide the 3,500 t DM of forage by 8.2 t DM/AU/year").
- If the biomass tool returns no data for the area, do not calculate: say so.
