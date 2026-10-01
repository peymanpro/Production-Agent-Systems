import unittest

import production_agent_systems


class FoundationTest(unittest.TestCase):
    def test_package_imports(self) -> None:
        self.assertEqual(production_agent_systems.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
