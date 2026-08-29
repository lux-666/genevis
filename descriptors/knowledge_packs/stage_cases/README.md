# Stage Cases V1

This directory is an isolated, repository-validated literature Case library.
It is not consumed by the production recommendation path yet.

- `sources/<paper_id>/` owns reviewed verbatim source text.
- `cases/<stage>/<case_id>.yaml` owns one technical arm for one sample and stage.
- `templates/` contains authoring skeletons; templates are not loaded as Cases.

Run `python -m pytest tests/test_case_library.py -q` after changing the library.
