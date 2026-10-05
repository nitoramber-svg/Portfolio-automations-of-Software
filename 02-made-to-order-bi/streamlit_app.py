"""Entry point: ``streamlit run streamlit_app.py`` (also what Streamlit Community Cloud runs)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from mto_bi.app.main import main  # noqa: E402

main()
