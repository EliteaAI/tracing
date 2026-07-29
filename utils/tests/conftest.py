import sys
import pathlib

# Ensure pytest can discover test_* files without traversing parent packages
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
