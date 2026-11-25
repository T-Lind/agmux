# agmux development helpers. `make install` is a thin wrapper over install.sh.
PREFIX ?= $(HOME)/.local
PYTHON ?= python3

.PHONY: install uninstall test unittest smoke lint man clean dist

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
	$(PYTHON) -m py_compile agmux.py agmux_discord.py tests/test_agmux.py tests/ui_smoke.py tests/fake_chat.py
	bash -n install.sh completion.bash

man:
	groff -Tutf8 -man agmux.1 >/dev/null

# Package only the committed release tree. Local configuration stays outside it.
DIST_NAME = agmux-$(shell git show HEAD:agmux.py | sed -n 's/^VERSION = "\(.*\)"/\1/p')
dist:
	mkdir -p dist
	git archive --format=tar.gz --prefix="$(DIST_NAME)/" -o "dist/$(DIST_NAME).tar.gz" HEAD
	cd dist && sha256sum "$(DIST_NAME).tar.gz" > "$(DIST_NAME).tar.gz.sha256"

clean:
	rm -rf __pycache__ tests/__pycache__
