"""
Indian VDA (Virtual Digital Asset) tax modeling for this project's
cross-exchange options spread.

NOT TAX ADVICE. This is a documented-assumptions calculator, not a
substitute for a chartered accountant. Confirm with a CA before trusting
any number here for a real trading/filing decision -- crypto options
specifically (vs. spot VDA, which is what most public guidance and most
CAs' working experience covers) is a genuinely less-tested area of Section
115BBH's application, and getting this wrong has real consequences.

ASSUMPTIONS THIS MODULE MAKES, STATED PLAINLY:

1. FLAT 30% TAX ON GAINS, PER SECTION 115BBH. Applied to the whole trade's
   net profit (both legs combined), not to each leg separately -- since the
   two legs are one economic position (a spread), not two independent
   assets, taxing them jointly is the defensible reading, but this is an
   interpretation, not a settled rule for this exact structure.

2. NO LOSS SET-OFF. Section 115BBH explicitly disallows setting off a loss
   from one VDA transaction against gains from another VDA transaction (or
   any other income). This means: if this specific trade loses money, that
   loss provides NO tax benefit elsewhere -- it doesn't reduce tax owed on
   a different winning trade, even within the same portfolio, same day,
   same underlying. This module reflects that by computing tax independently
   per trade, never netting against anything else.

3. NO EXPENSE DEDUCTION beyond cost of acquisition. Exchange fees ARE
   generally treated as part of cost of acquisition/transfer under current
   guidance (reducing the taxable gain), which is why fees are already
   subtracted before this module's tax calculation runs (see
   pricing/manual_spread_finder.py's net_entry_cost, which is fee-adjusted
   upstream) -- but nothing else (electricity, internet, this software) is
   deductible against VDA gains, unlike a normal business expense.

4. 1% TDS (Section 194S) IS FLAGGED, NOT SUBTRACTED FROM THE HEADLINE
   NUMBER. TDS is an advance-tax withholding at the point of transfer
   (deducted by the exchange, or by the buyer/seller directly in a P2P-like
   structure), not an additional cost on top of the 30% tax -- it's
   credited against the 30% liability when the return is filed. This
   module reports it separately (tds_withheld_estimate) rather than
   subtracting it from net_profit_after_tax, since double-counting it as
   both "withheld now" and "owed later" would understate the real economics.
   The 1% threshold/applicability (₹50,000/₹10,000 depending on payer
   category, per Section 194S) is ALSO not definitively resolved for a
   cross-exchange manual trade structure like this one -- flagged, not
   assumed.

5. SETTLEMENT-TIME REALIZATION ASSUMED. This module assumes both legs are
   treated as realized (gain or loss crystallized) at the SHORT leg's
   settlement, consistent with how pricing/manual_spread_finder.py already
   frames net_entry_cost as the trade's economic outcome at that point.
   Whether tax law actually treats a still-open long leg as "realized" at
   that moment, vs. only upon its own later settlement/sale, is exactly the
   kind of question a CA should confirm for this specific structure.

Given all of the above, treat this module's output as "a reasonable,
clearly-stated first-pass estimate for comparing candidates against each
other," not as "the tax you will actually owe."
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Section 115BBH, confirmed stable since introduction (FY 2022-23) --
# unlike the assumptions above, this rate itself is not in question.
VDA_TAX_RATE = Decimal("0.30")

# Section 194S -- see module docstring point 4 for why this is reported
# separately rather than netted into net_profit_after_tax.
VDA_TDS_RATE = Decimal("0.01")


@dataclass(frozen=True)
class TaxEstimate:
    gross_profit: Decimal          # profit before tax (post-fee, e.g. net_entry_cost realized favorably)
    tax_owed_estimate: Decimal     # 0 if gross_profit <= 0 -- no loss set-off means no negative tax either
    net_profit_after_tax: Decimal  # gross_profit - tax_owed_estimate
    tds_withheld_estimate: Decimal # informational only, see module docstring point 4 -- NOT subtracted above
    effective_tax_rate: Decimal    # tax_owed_estimate / gross_profit, 0 if gross_profit <= 0


def estimate_vda_tax(gross_profit: Decimal, contract_value_for_tds: Decimal | None = None) -> TaxEstimate:
    """
    gross_profit: the trade's pre-tax profit (already fee-adjusted -- see
    module docstring point 3). Pass net_entry_cost's favorable realization,
    not a raw price.

    contract_value_for_tds: the transaction value TDS would apply against
    (typically the sell-side proceeds) -- optional, since TDS is informational
    only here (see docstring point 4). If omitted, tds_withheld_estimate is 0.

    Per docstring point 2: if gross_profit <= 0, tax_owed_estimate is 0 --
    NOT negative. This trade's loss cannot offset anything else, so there is
    no tax benefit to compute, and returning a negative number here would
    wrongly imply one exists.
    """
    if gross_profit > 0:
        tax_owed = (gross_profit * VDA_TAX_RATE).quantize(Decimal("0.01"))
        effective_rate = VDA_TAX_RATE
    else:
        tax_owed = Decimal("0")
        effective_rate = Decimal("0")

    net_after_tax = gross_profit - tax_owed

    tds = Decimal("0")
    if contract_value_for_tds is not None and contract_value_for_tds > 0:
        tds = (contract_value_for_tds * VDA_TDS_RATE).quantize(Decimal("0.01"))

    return TaxEstimate(
        gross_profit=gross_profit,
        tax_owed_estimate=tax_owed,
        net_profit_after_tax=net_after_tax,
        tds_withheld_estimate=tds,
        effective_tax_rate=effective_rate,
    )
