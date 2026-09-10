# Build context is the repository root (see docker-compose.yml's
# `slack-gateway` / `slack-gateway-test` services), not this directory.
# As of the Orchestrator dispatch rewiring this package has a local
# sibling-package dependency (packages/agent-contracts, for AgentRequest /
# AgentResponse) that must be installed first: there is no private package
# index behind `local-agent-concierge-agent-contracts`'s plain package
# name, so install order is load-bearing. This mirrors
# services/orchestrator/Dockerfile, which already builds from the repo
# root for exactly the same reason.
FROM python:3.12-slim AS base

WORKDIR /app

COPY packages/agent-contracts /packages/agent-contracts
RUN pip install --no-cache-dir /packages/agent-contracts

COPY apps/slack-gateway/pyproject.toml ./pyproject.toml
COPY apps/slack-gateway/src ./src


FROM base AS test

RUN pip install --no-cache-dir -e ".[test]"

COPY apps/slack-gateway/tests ./tests

CMD ["python", "-m", "pytest"]


FROM base AS runtime

RUN pip install --no-cache-dir .

CMD ["python", "-m", "slack_gateway"]
