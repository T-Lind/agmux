# agmux development helpers. `make install` is a thin wrapper over install.sh.
PREFIX ?= $(HOME)/.local
PYTHON ?= python3

.PHONY: install uninstall test unittest smoke lint man clean

install:
	./install.sh --bin "$(PREFIX)/bin"

uninstall:
	./install.sh --uninstall --bin "$(PREFIX)/bin"

test: lint unittest smoke

unittest:
	$(PYTHON) -m unittest discover -s tests -v

smoke:
	$(PYTHON) tests/ui_smoke.py

lint:
	$(PYTHON) -m py_compile agmux.py tests/test_agmux.py tests/ui_smoke.py
	bash -n install.sh completion.bash

man:
	groff -Tutf8 -man agmux.1 >/dev/null

clean:
	rm -rf __pycache__ tests/__pycache__
