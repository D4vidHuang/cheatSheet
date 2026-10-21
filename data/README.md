# Local data boundary

Place the supplied benchmark at `data/SWE-Refactor/`, including
`pure_refactoring_data.json`. This directory is deliberately ignored by Git.
The supplied originals were preserved unchanged.

The authoritative local input is the root JSON, not the incomplete per-project
JSON copies in `code/data/` and not the old result spreadsheets.

Upstream sources:

- Paper: https://arxiv.org/abs/2602.03712
- Archive record inspected: https://zenodo.org/records/22001546 (v3; the page indicates a newer version exists).

The local archive's download version is unverified. Its SHA-256 in
`reports/dataset_audit.json` is the experiment identity. Do not replace it with a
newer archive during a frozen experiment. Obtain data from its owners and verify
that exact SHA before using a different checkout; no archive, third-party Java
source, or reference answer is redistributed by this repository.

Run `python3 scripts/audit_dataset.py --help` to recreate the compact audit.
Original source licenses remain applicable. A license for our new code has not
been selected; this repository does not grant new rights to third-party data.
