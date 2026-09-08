# Entry points a newcomer needs, in the order they need them.
.PHONY: help bootstrap creds selftest list steps check run audit
help:            ## this list
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F':.*##' '{printf "  %-10s %s\n",$$1,$$2}'
bootstrap:       ## once per checkout: python dep + the etcd binaries the claim path ships
	python3 -m pip install -q -r requirements.txt
	./instant-cluster/fetch-etcd.sh
creds:           ## 5s: does Prism Central accept the credentials in nkp-e2e.env?
	./pc_creds.py
selftest:        ## ~1s, no cluster, no creds: the framework is intact
	python3 selftest.py
list:            ## scenarios available
	./run_e2e.py --list
steps:           ## every step a scenario may use
	./run_e2e.py --steps
check:           ## validate a scenario before it costs a cluster: make check S=<name>
	./check_scenario.py $(S)
run:             ## run one live: make run S=<name> [ARGS=--keep]
	./run_e2e.py $(S) $(ARGS)
audit:           ## refuse to publish credentials or internal environment details
	./audit_public.sh
