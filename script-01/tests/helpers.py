import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a saved API response fixture by name (without .json extension)."""
    with open(FIXTURES / f"{name}.json") as f:
        return json.load(f)
