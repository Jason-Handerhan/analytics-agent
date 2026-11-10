FROM node:24-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

ARG DATAFORM_CLI_VERSION
ARG DATAFORM_CORE_VERSION
RUN npm install -g @dataform/cli@${DATAFORM_CLI_VERSION} \
    && npm install --prefix /tmp/core-warmup @dataform/core@${DATAFORM_CORE_VERSION} \
    && rm -rf /tmp/core-warmup

# The ML repo's requirements.txt, with every version held to the VM's by requirements.lock (a full freeze of the
# Vertex Workbench environment, minus the packages that exist only on that VM). gcsfs is imported by the code but not listed.
ARG LOCK_COMMIT
COPY requirements.txt requirements.lock /tmp/
RUN grep -vE '^(nb_micromamba_kernels|vertex_ai_workbench|wbi_)' /tmp/requirements.lock > /tmp/constraints.txt \
    && uv venv --python 3.12.13 /opt/ml-venv \
    && UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=4 uv pip install --python /opt/ml-venv/bin/python -r /tmp/requirements.txt gcsfs pytest ruff nbformat -c /tmp/constraints.txt \
    && echo "${LOCK_COMMIT}" > /opt/lock-commit \
    && cat /tmp/requirements.txt /tmp/requirements.lock | sha256sum | cut -d" " -f1 > /opt/inputs-sha256
ENV PATH=/opt/ml-venv/bin:$PATH
