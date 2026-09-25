import unittest

from roman import from_roman, to_roman


class RomanTest(unittest.TestCase):
    def test_known_values(self):
        for n, s in [(1, "I"), (4, "IV"), (9, "IX"), (14, "XIV"), (40, "XL"),
                     (90, "XC"), (400, "CD"), (1994, "MCMXCIV"), (3999, "MMMCMXCIX")]:
            self.assertEqual(to_roman(n), s)
            self.assertEqual(from_roman(s), n)

    def test_round_trip(self):
        for n in range(1, 4000):
            self.assertEqual(from_roman(to_roman(n)), n)

    def test_invalid(self):
        for bad in ["", "IIII", "IC", "VX", "MMMM", "ABC"]:
            with self.assertRaises(ValueError):
                from_roman(bad)
        for bad in [0, 4000, -1]:
            with self.assertRaises(ValueError):
                to_roman(bad)


if __name__ == "__main__":
    unittest.main()
