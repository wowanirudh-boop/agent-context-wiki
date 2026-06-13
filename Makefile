PYTHON ?= python
VENV := .venv

ifeq ($(OS),Windows_NT)
VENV_BIN := $(VENV)/Scripts
VENV_PYTHON := $(VENV_BIN)/python.exe
VENV_PYTEST := $(VENV_BIN)/pytest.exe
VENV_RUFF := $(VENV_BIN)/ruff.exe
else
VENV_BIN := $(VENV)/bin
VENV_PYTHON := $(VENV_BIN)/python
VENV_PYTEST := $(VENV_BIN)/pytest
VENV_RUFF := $(VENV_BIN)/ruff
endif

export ACW_LLM_PROVIDER ?= fake-rules

.PHONY: setup check test lint mcp-import-smoke

setup:
	$(PYTHON) -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 'Python 3.11+ is required')"
	$(PYTHON) -c "from pathlib import Path; import subprocess, sys; p = Path(r'$(VENV_PYTHON)'); raise SystemExit(0 if p.exists() else subprocess.call([sys.executable, '-m', 'venv', r'$(VENV)']))"
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -r api/requirements.txt
	$(VENV_PYTHON) -m pip install -r mcp/requirements.txt
	$(VENV_PYTHON) -m pip install -r core/requirements.txt
	$(VENV_PYTHON) -m pip install -r tests/requirements.txt
	$(VENV_PYTHON) -m pip install ruff pytest pytest-asyncio pytest-cov hypothesis markdown-it-py
	$(VENV_PYTHON) -c "from pathlib import Path; import shutil; src = Path(r'$(VENV)/Scripts/python.exe'); dst = Path(r'$(VENV)/bin/python.exe'); dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst) if src.exists() else None"

lint: setup
	$(VENV_RUFF) check .

mcp-import-smoke:
	$(PYTHON) -c "import sys; sys.path.insert(0, 'mcp'); from mcp.server.fastmcp import FastMCP; from tools import register; register(FastMCP('CI import smoke'), lambda ctx: 'ci', lambda user_id: None)"

test: setup
	$(VENV_PYTEST) tests/unit -q
	$(VENV_PYTHON) -c "from pathlib import Path; import subprocess, sys; raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest', 'tests/v2', '-q', '--cov=core', '--cov-fail-under=0']) if Path('tests/v2').is_dir() else 0)"

check: setup lint test
	$(VENV_PYTHON) -c "from pathlib import Path; import subprocess, sys; raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest', 'tests/v2', '-q', '--cov=core', '--cov-fail-under=85']) if Path('core/pipeline/run.py').is_file() else 0)"
