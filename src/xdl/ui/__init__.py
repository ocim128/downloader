"""UI subpackage: the localhost web frontend (`xdl serve`, docs/technical/10).

Frontend per layering rule 1: imports `app`/`config`/`report` shapes only.
The orchestrator/analyze/store surfaces are injected callables, so this module
works against the documented contract with a stub (tests) and the real pipeline
post-merge (python -m xdl.ui).
"""
