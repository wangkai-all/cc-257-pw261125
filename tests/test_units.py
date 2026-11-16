"""Exact arithmetic: no float drift, no ambiguous decimal marks."""

import unittest

from tests.base import TempDB  # noqa: F401  (path setup)
from app.canon import q, qs
from app.units import to_base, dimension_of, UnknownUnit


class DecimalParsing(unittest.TestCase):
    def test_dot_and_comma_are_explicit(self):
        self.assertEqual(qs(q("1.234", "dot")), "1.234")
        self.assertEqual(qs(q("1.234", "comma")), "1234")
        self.assertEqual(qs(q("2,5", "comma")), "2.5")
        self.assertEqual(qs(q("1.234,56", "comma")), "1234.56")
        self.assertEqual(qs(q("1,234.56", "dot")), "1234.56")

    def test_signs_and_currency(self):
        self.assertEqual(qs(q("(12.50)", "dot")), "-12.50")
        self.assertEqual(qs(q("-3", "dot")), "-3")
        self.assertEqual(qs(q("€ 4,50", "comma")), "4.50")
        self.assertIsNone(q("", "dot"))

    def test_no_float_rounding_on_large_values(self):
        value = q("9007199254740993", "dot")
        self.assertEqual(qs(value), "9007199254740993")
        self.assertNotEqual(qs(value), qs(q(str(int(float(9007199254740992))))))
        self.assertEqual(qs(q("0.1", "dot") + q("0.2", "dot")), "0.3")


class UnitConversion(unittest.TestCase):
    def test_exact_conversion(self):
        amount, unit, note = to_base(q("2.5"), "kg", scale=3)
        self.assertEqual((amount, unit), ("2500.000", "g"))
        self.assertEqual(note["factor"], "1000/1")
        self.assertEqual(note["method"], "exact_rational")

    def test_half_even_rounding_is_declared(self):
        # 1 inch = 25.4 mm exactly; 0.1 inch halves at scale 1 -> 3 mm? No:
        # 2.54 -> 2.5 (half-even to 1 decimal is 2.5 exactly, no rounding).
        # Use a value that genuinely halves: 0.25 in = 6.35 mm at scale 1.
        amount, _unit, note = to_base(q("0.25"), "in", scale=1)
        self.assertEqual(amount, "6.4")  # 6.35 -> half-even with 4 even => 6.4
        self.assertEqual(note["rounding"], "half_even")

    def test_inch_conversion_is_exact_rational(self):
        amount, _unit, note = to_base(q("1"), "in", scale=9)
        self.assertEqual(amount, "25.400000000")
        self.assertEqual(note["factor"], "127/5")

    def test_unknown_unit_is_explicit(self):
        with self.assertRaises(UnknownUnit):
            to_base(q("1"), "furlong")

    def test_dimension_classification(self):
        self.assertEqual(dimension_of("KG"), "mass")
        self.assertEqual(dimension_of("pcs"), "count")
        self.assertEqual(dimension_of("m"), "length")
        self.assertIsNone(dimension_of("widgets"))


class RoundTrip(unittest.TestCase):
    def test_locale_precision_survives(self):
        value = q("123456789.123456789", "dot")
        self.assertEqual(qs(value), "123456789.123456789")


if __name__ == "__main__":
    unittest.main()
