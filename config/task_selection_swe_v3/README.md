# SWE task selection v3 (immutable)

Frozen package for **swe-0.3.5**. Do not edit, rerun, or regenerate after the
first episode. The draw is the co-author `swe_selection_v3` draw.

## Reproduce (IDs and hash only)

From this directory, against the read-only v0.3 local dataset (not copied here;
it contains gold patches):

```sh
python3 select_swe_tasks_v3.py \
  --local <REPO> \
  --out swe_task_selection_v3.json \
  --pilot-out swe_pilot.json
```

`selection_sha256` must be
`e426590d9e9342af60ab48c5e3ddb174a73fbdfb05de5fff7ffb7d348752f0be`.
The script exits if the prefix is not `<commit>`.

Hashed payload (canonical): `seed`, `filter`, `exclusions`, main IDs, reserve IDs.
Table fields (`files`, `changed_lines`, `src_lines_changed`) and
`study_assignment` are not in the hash.

## Provenance

| Item | SHA-256 |
|---|---|
| As-received `select_swe_tasks_v3.py` | `74a04225a510a43c09831c60ebb8f691a858025f5579121ee4fe5611dff8de2d` |
| Copied v0.3 `select_swe_tasks.py` (eligible / draw) | `5e89e1b5d2e2ba2ef4ae1a6662c0df92fa178e0dd4e6cc76fd07c1cfd26c52e7` |
| `swe_verified_local.json` | `7d4f90f4781e756e9e3df04f8372b1e19235bd678321e72c39bdde32cb63ce9b` |
| As-received JSON (`files: null`) | `ad818d5d814dbf26ab35cf252d196f635404a75b7d7e3491bda2f7e485394ef5` |
| `selection_sha256` | `e426590d9e9342af60ab48c5e3ddb174a73fbdfb05de5fff7ffb7d348752f0be` |

Source: `/tmp/for_coauthor_swe/for_coauthor_swe/swe_selection_v3/`.
Seed `<commit>`. Filter R1–R9 at v0.3 thresholds (A-2 tightening not applied).
Repo cap 0.25. `matplotlib/matplotlib` excluded as infrastructure (3/3 harness
timeouts). All previously exposed IDs excluded.

The as-received table had `files: null` because `base.files_touched` does not
exist. This package fills file/line counts from `patch_stats` after the draw.
The draw was not changed.

## swe-0.3.5 assignment

| Role | IDs (in order) |
|---|---|
| Pilot | `astropy__astropy-7606`, `sympy__sympy-19783`, `django__django-13112` (first 3 reserve) |
| Main | 8 v3 main IDs below |
| Remaining reserve | 5, in order, Gate 0 replacements only |

### Main

| ID | difficulty | files | changed lines | src lines |
|---|---|---:|---:|---:|
| `pylint-dev__pylint-6386` | 15 min - 1 hour | 4 | 15 | 15 |
| `sphinx-doc__sphinx-8593` | 15 min - 1 hour | 2 | 60 | 60 |
| `sympy__sympy-12419` | 15 min - 1 hour | 1 | 10 | 10 |
| `django__django-14792` | <15 min fix | 1 | 7 | 7 |
| `sympy__sympy-16766` | <15 min fix | 1 | 5 | 5 |
| `scikit-learn__scikit-learn-14087` | 15 min - 1 hour | 1 | 9 | 9 |
| `astropy__astropy-8872` | 15 min - 1 hour | 1 | 11 | 11 |
| `django__django-11292` | 15 min - 1 hour | 1 | 11 | 11 |

### Reserve (in order)

| ID | role | difficulty | files | changed lines | src lines |
|---|---|---|---:|---:|---:|
| `astropy__astropy-7606` | pilot | 15 min - 1 hour | 1 | 10 | 10 |
| `sympy__sympy-19783` | pilot | 15 min - 1 hour | 2 | 13 | 13 |
| `django__django-13112` | pilot | <15 min fix | 1 | 6 | 6 |
| `django__django-14155` | Gate 0 only | 15 min - 1 hour | 1 | 13 | 13 |
| `sphinx-doc__sphinx-11445` | Gate 0 only | 15 min - 1 hour | 1 | 14 | 14 |
| `scikit-learn__scikit-learn-12682` | Gate 0 only | 15 min - 1 hour | 2 | 90 | 90 |
| `pytest-dev__pytest-7490` | Gate 0 only | 15 min - 1 hour | 1 | 21 | 21 |
| `astropy__astropy-14096` | Gate 0 only | 15 min - 1 hour | 1 | 6 | 6 |

Eligible pool: 117. Main repo mix: pylint 1, sphinx 1, sympy 2, django 2,
scikit-learn 1, astropy 1.
