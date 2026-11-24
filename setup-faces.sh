#!/bin/bash
# Optional: install the libraries for "Group by faces". The dashboard also does this
# automatically the first time you press Group by faces, so you rarely need to run it by hand.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || ./setup.sh || exit 1
echo "Installing face-grouping libraries (one-time, a few hundred MB)..."
.venv/bin/pip install -q -r requirements-faces.txt \
  && echo "Done. Press Group by faces in the dashboard."
