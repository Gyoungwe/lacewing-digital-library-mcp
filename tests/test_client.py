import unittest
from unittest.mock import patch

from ldl_mcp.client import LDLClient


class ResolveCurrentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = LDLClient(delay=0)
        self.rows = [
            {
                "CombObjID": 17520,
                "CombGenuToSubs": "Mantis styriaca",
                "PresCombGenu": "Mantispa",
                "PresCombSpec": "styriaca",
                "CombTaxStatus": "valid",
            },
            {
                "CombObjID": 6916,
                "CombGenuToSubs": "Mantispa styriaca",
                "PresCombGenu": "Mantispa",
                "PresCombSpec": "styriaca",
                "CombTaxStatus": "valid",
            },
            {
                "CombObjID": 25023,
                "CombGenuToSubs": "Mantispa styriaca pagana",
                "PresCombGenu": "Mantispa",
                "PresCombSpec": "styriaca",
                "CombTaxStatus": "invalid, jr. syn.",
            },
        ]

    def test_resolve_current_normalizes_case_and_whitespace(self) -> None:
        with patch.object(self.client, "search_name", return_value=self.rows):
            result = self.client.resolve_current("  mantispa   STYRIACA ")

        self.assertIsNotNone(result)
        self.assertEqual(result["CombObjID"], 6916)

    def test_resolve_current_returns_none_for_blank_name(self) -> None:
        with patch.object(self.client, "search_name") as search_name:
            self.assertIsNone(self.client.resolve_current(" \t"))

        search_name.assert_not_called()


if __name__ == "__main__":
    unittest.main()
