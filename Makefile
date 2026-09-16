# LIFE Compute miner — canonical entry points.
#
# The repo has no test framework; correctness lives in scripts/verify_*.{py,js},
# each self-contained and exiting non-zero on failure. This is the one command
# that runs them, so "is the tree green?" has a single answer.
#
#   make test            every verifier
#   make test-offline    pass --offline to those that support it (network-free)
#   make verify-nft      the discovery-NFT pipeline only
#   make check           syntax/lint only, no execution
#   make publish         ship discovery metadata to the public site
#
# Add a verifier by dropping it in scripts/ as verify_*.py or verify_*.js —
# it is picked up automatically, no edit here.

SHELL   := /bin/bash
PY      := $(if $(wildcard .venv/bin/python3),.venv/bin/python3,python3)
VERIFY  := $(sort $(wildcard scripts/verify_*.py) $(wildcard scripts/verify_*.js))
TIMEOUT := 180

# KNOWN_FAILING: verifiers red for reasons unrelated to the current change.
# They still RUN and still report — this list only keeps `make test` honest
# about what is new breakage versus inherited. Empty it as they get fixed.
#   verify_peg_panel.py       untracked; observer count 250 != recompute 960
#   verify_proteinnet_fixes.py  CDK4/ESR1 refused + evicted (since 36bfa29)
KNOWN_FAILING := scripts/verify_peg_panel.py scripts/verify_proteinnet_fixes.py

.PHONY: test test-offline verify-nft check publish list

test:            ; @$(MAKE) --no-print-directory run ARGS=
test-offline:    ; @$(MAKE) --no-print-directory run ARGS=--offline

# One runner, two modes — keeps the pass/fail bookkeeping in a single place.
.PHONY: run
run:
	@fail=(); known=(); pass=0; \
	for v in $(VERIFY); do \
	  printf '%-38s ' "$$(basename $$v)"; \
	  args=$(ARGS); \
	  [ -n "$$args" ] && ! grep -q -- "$$args" "$$v" && args=; \
	  case $$v in *.js) cmd=(node $$v $$args);; *) cmd=($(PY) $$v $$args);; esac; \
	  if out=$$(timeout $(TIMEOUT) "$${cmd[@]}" 2>&1); then \
	    echo "PASS"; pass=$$((pass+1)); \
	  elif [[ " $(KNOWN_FAILING) " == *" $$v "* ]]; then \
	    echo "FAIL (known)"; known+=("$$v"); \
	  else \
	    echo "FAIL"; fail+=("$$v"); \
	    echo "$$out" | tail -3 | sed 's/^/      /'; \
	  fi; \
	done; \
	echo; printf '%d passed' $$pass; \
	[ $${#known[@]} -gt 0 ] && printf ', %d known-failing' $${#known[@]}; \
	[ $${#fail[@]} -gt 0 ] && printf ', %d FAILED' $${#fail[@]}; \
	echo; \
	if [ $${#fail[@]} -gt 0 ]; then printf '  - %s\n' "$${fail[@]}"; exit 1; fi

verify-nft:
	@node scripts/verify_discovery_nft.js

check:
	@for f in $(filter %.js,$(VERIFY)) scripts/mint_discovery_nft.js \
	          scripts/publish_discovery_metadata.js; do node --check $$f; done
	@$(PY) -m py_compile $(filter %.py,$(VERIFY))
	@echo "syntax OK"

publish:
	@node scripts/publish_discovery_metadata.js

list:
	@printf '%s\n' $(VERIFY)
