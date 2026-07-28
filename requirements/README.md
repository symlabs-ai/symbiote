# Linux dependency locks

These files make the Gitea Linux gate and the staging Docker image deterministic.
They are not constraints for consumers installing the Symbiote library on other
platforms.

- `bootstrap-linux.lock`: build frontend and backends.
- `ci-linux.lock`: test, lint, runtime and ForgeLLM dependencies.
- `runtime-linux.lock`: staging runtime and ForgeLLM dependencies.

Refresh procedure:

1. Resolve from a clean Python 3.12 virtual environment.
2. Keep ForgeLLM pinned to its immutable release commit in both `pyproject.toml`
   and the lock files.
3. Generate the CI set from `.[dev,llm]` and the runtime set from `.[llm]`.
4. Run the complete suite, Ruff, `pip check`, the image contract and the health
   canary before committing all lock changes together.

Do not refresh lock files implicitly during CI or image builds.
