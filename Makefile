PYTHON ?= .venv/bin/python
RUNNER_FLAGS ?=

.PHONY: eval eval-one test

eval:
	$(PYTHON) -m eval.runner --all $(RUNNER_FLAGS)
	$(PYTHON) -m eval.baseline --run latest
	$(PYTHON) -m eval.score --run latest
	$(PYTHON) -m eval.update_readme

eval-one:
	$(PYTHON) -m eval.runner --scenario $(SCENARIO) --debounce-override 0
	$(PYTHON) -m eval.baseline --run latest
	$(PYTHON) -m eval.score --run latest

test:
	$(PYTHON) -m pytest tests/ -q
