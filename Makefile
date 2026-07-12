PYTHON ?= .venv/bin/python
RUNNER_FLAGS ?=

.PHONY: eval eval-one eval-agent test

eval:
	$(PYTHON) -m eval.runner --all $(RUNNER_FLAGS)
	$(PYTHON) -m eval.baseline --run latest
	$(PYTHON) -m eval.score --run latest
	$(PYTHON) -m eval.update_readme

eval-one:
	$(PYTHON) -m eval.runner --scenario $(SCENARIO) --debounce-override 0
	$(PYTHON) -m eval.baseline --run latest
	$(PYTHON) -m eval.score --run latest

# Real agent over the latest recorded run (overwrites that run's agent_output
# files, rescores, republishes the README table — provenance says who ran).
eval-agent:
	$(PYTHON) -m agent.evalrun --run latest
	$(PYTHON) -m eval.score --run latest
	$(PYTHON) -m eval.update_readme

test:
	$(PYTHON) -m pytest tests/ -q
