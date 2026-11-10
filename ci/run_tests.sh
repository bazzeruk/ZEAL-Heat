#!/bin/sh
# Install one pinned test environment and run the suite. REQS names the
# requirements file. Jenkins runs the container as its own user, whose
# default home is not writable, so pip's home and cache live in the workspace.
set -eu
: "${REQS:?set REQS to a requirements file}"
export HOME="$PWD" PIP_CACHE_DIR="$PWD/.pip-cache"
python -m venv .venv
.venv/bin/python -m pip install -q -r "$REQS"
.venv/bin/python -m pytest tests/ -q --junitxml=test-results.xml
.venv/bin/python -m compileall -q custom_components tests
