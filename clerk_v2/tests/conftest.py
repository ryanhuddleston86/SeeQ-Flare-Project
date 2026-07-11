import sys
from pathlib import Path

# Add clerk/ outer directory to sys.path so `from clerk.schemas import …` resolves
# to clerk/clerk/schemas.py regardless of where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).parent.parent))
