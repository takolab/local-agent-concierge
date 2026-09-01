# Build context is the repository root (see docker-compose.yml's
# `orchestrator`/`orchestrator-test` services), not this directory --
# unlike apps/slack-gateway and mcp/google-calendar-mcp, this package has a
# local sibling-package dependency (packages/agent-contracts) that must be
# installed first. There is no private package index behind
# `local-agent-concierge-agent-contracts`'s plain package name, so install
# order is load-bearing -- see docs/orchestrator/domain-model.md
# "Installation". Building from the repo root lets this Dockerfile
# reproduce that same two-step order (agent-contracts, then
# services/orchestrator) that docs/orchestrator/domain-model.md and
# .github/workflows/orchestrator.yml already use.
FROM python:3.12-slim AS base

WORKDIR /app

COPY packages/agent-contracts /packages/agent-contracts
RUN pip install --no-cache-dir /packages/agent-contracts

COPY services/orchestrator/pyproject.toml ./pyproject.toml
COPY services/orchestrator/src ./src


FROM base AS test

RUN pip install --no-cache-dir -e ".[test]"

COPY services/orchestrator/tests ./tests

CMD ["python", "-m", "pytest"]


FROM base AS runtime

RUN pip install --no-cache-dir .

EXPOSE 8700

CMD ["python", "-m", "orchestrator"]
