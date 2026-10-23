---
name: ua-calculator
description: Animal Unit (AU) scenario - actual stocking rate of a registered pasture and an order-of-magnitude upper-bound scenario of how many animals the dry-matter production could feed.
license: GPL-3.0-or-later
metadata:
  version: "1.1.0"
  tags: ["animal unit", "stocking rate", "scenario"]
---

# Animal Unit (AU) scenario

Use this skill to compute the actual stocking rate of a herd, to convert a herd into Animal Units (AU), and to give an order-of-magnitude **upper-bound scenario** of how many AU the dry-matter production of a registered pasture could feed. It is a simple demonstration based on global satellite data; local policies, breeds, management and field data would refine it. The result is a scenario, NOT the real carrying capacity of the area.

## When to Use

- The user asks for the stocking rate of an area, or whether the area could support the current herd.
- The user asks how many animals a pasture could feed (carrying capacity).
- The user wants a herd converted to Animal Units.

## Process

1. **Get the dry-matter production**: call `get_biomass_total` for the registered area. Note `mean_t_ha`, `total_t`, the productive pasture hectares, the period and its number of days, and the source (Global Pasture Watch annual or Time2Graze). Prefer the annual Global Pasture Watch year (the default of the tool). Use Time2Graze only when the user asks for recent data or no annual data exists.
2. **CRITICAL - check the period before any calculation**: the formulas below need an ANNUAL total of dry matter (t DM/year).
   - If the data is a full Global Pasture Watch year (365 or 366 days), use `total_t` as is.
   - If it is a Time2Graze period (a month or part of a year), annualize with the factor 365 / days covered (days are reported with the data): annual total = total_t x 365 / days covered. NEVER multiply a month by 12 and never skip this check. Tell the user explicitly that the scenario extrapolates a short period to a year and may be far from the real annual production (growth is seasonal).
3. **Say what the figure is**: the annual dry-matter production is the matter the pasture produces in the period, not the forage available to animals at a given moment. The scenario treats that production as the forage supply and applies an explicit utilization factor; label the result "upper-bound scenario (assumes the annual dry-matter production is the forage supply)", never "the carrying capacity of the area".
4. **Collect the herd** when the stocking rate is needed: number of animals and average live weight, so the total live weight (kg) is known. Ask for it if missing. If the user already gives the herd in AU, use it directly and skip the conversion.
5. **State the assumptions** (see below) and let the user override them.
6. **Calculate with the calculator tools**, step by step, one binary operation per call (multiply, divide...). Never compute in your head.
7. **Explain didactically in plain text**: show the formula with the numbers substituted (for example "450 kg x 100 animals = 45,000 kg"), the period and source of the dry matter, whether it was annualized, and a short comparison between the actual stocking rate and the scenario. Do NOT use LaTeX, math blocks or dollar signs; write formulas as plain text with x and /. Present results as an order of magnitude, not as a measurement.

## Formulas

- 1 AU = 450 kg of live weight.
- Total AU of the herd = total live weight (kg) / 450
- Actual stocking rate (AU/ha) = total AU / pasture area (ha), using the productive pasture hectares of the biomass result.
- Annual demand per AU = 450 kg x 2.5% per day x 365 days = 4.106 t DM/AU/year (about 4.1).
- Utilization factor 50%: only about half of the production can be used by the animals; the rest stays as residue, trampling losses and regrowth. Demand with the utilization factor = 4.106 / 0.5 = 8.2125, rounded to 8.2 t DM/AU/year (use 8.2 in the calculations and say it is rounded).
- Upper-bound scenario (AU) = annual dry-matter production (t DM/year) / 8.2
- Upper-bound scenario per hectare (AU/ha) = scenario (AU) / pasture area (ha)
- Always divide by the productive pasture hectares of the biomass result (`pasture_ha`), not by the whole property or registered area.

## Assumptions to declare (and accept overrides)

- 1 AU = 450 kg live weight (a reference animal; other values can be used if the user gives them).
- Daily dry-matter intake of 2.5% of live weight.
- Utilization factor of 50% (the demand with it is 8.2 t DM/AU/year).
- The dry-matter figures come from a general productivity-to-dry-matter conversion calibrated for cultivated tropical pastures; elsewhere they are only an order of magnitude.

## Best Practices

- Always say which period and source the dry matter came from, and whether it was annualized.
- Never promise yields, performance or a safe stocking rate: the scenario does not replace field verification or local technical advice.
- Show the substituted numbers in plain text (for example: "we divide the 3,500 t DM of production by 8.2 t DM/AU/year").
- If the biomass tool returns no data for the area, do not calculate: say so.
