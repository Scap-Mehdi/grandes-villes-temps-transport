#!/usr/bin/env python3
"""Checks of the sales filtering and of the price per cell (build_prices.py). Usage: python3 tools/test_prices.py"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_prices import MIN_SALES, cell_prices, sales_of_mutations  # noqa: E402

FIELDS = ["id_mutation", "date_mutation", "numero_disposition", "nature_mutation", "valeur_fonciere", "type_local",
          "surface_reelle_bati", "nombre_pieces_principales", "lot1_numero", "longitude", "latitude"]


def row(mutation, type_local="Appartement", value="300000", surface="50", nature="Vente", disposition="1", lot="1"):
    values = [mutation, "2025-01-01", disposition, nature, value, type_local, surface, "2", lot, "2.35", "48.85"]
    return dict(zip(FIELDS, values))


class SalesOfMutations(unittest.TestCase):
    def test_price_per_square_metre(self):
        sales = sales_of_mutations([row("a", value="300000", surface="50")])
        self.assertEqual(len(sales), 1)
        self.assertAlmostEqual(sales[0][3], 6000)

    def test_decimal_comma(self):
        self.assertAlmostEqual(sales_of_mutations([row("a", value="300000,50", surface="50")])[0][3], 6000.01)

    def test_same_local_on_two_parcels_counts_once(self):
        self.assertEqual(len(sales_of_mutations([row("a"), row("a")])), 1)

    def test_two_apartments_in_one_deed_are_skipped(self):
        self.assertEqual(sales_of_mutations([row("a", lot="1"), row("a", lot="2", surface="30")]), [])

    def test_deed_with_a_house_or_a_shop_is_skipped(self):
        self.assertEqual(sales_of_mutations([row("a"), row("a", type_local="Maison", surface="100")]), [])
        self.assertEqual(sales_of_mutations([row("a"), row("a", type_local="Local industriel. commercial ou assimilé", surface="20")]), [])

    def test_only_sales(self):
        self.assertEqual(sales_of_mutations([row("a", nature="Echange")]), [])

    def test_outliers_are_dropped(self):
        self.assertEqual(sales_of_mutations([row("a", value="10000", surface="50")]), [])  # 200 €/m²
        self.assertEqual(sales_of_mutations([row("b", value="300000", surface="5")]), [])  # 5 m²


class CellPrices(unittest.TestCase):
    def test_median_of_nearby_sales_and_fallback(self):
        sales = [(float(i), 0.0, 6000.0 + i) for i in range(MIN_SALES * 2)]
        prices, counts = cell_prices(sales, [(10.0, 0.0), (50_000.0, 0.0)], [])
        self.assertTrue(6000 <= prices[0] <= 6040)
        self.assertGreaterEqual(counts[0], MIN_SALES)
        self.assertEqual((prices[1], counts[1]), (0, 0))  # nothing within 1.5 km, no borough: no data


if __name__ == "__main__":
    unittest.main()
