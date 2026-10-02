FROM python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
RUN apt-get update && apt-get install -y --no-install-recommends git procps && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple pytest==8.4.2 mypy==1.18.2 typing_extensions==4.15.0 flit_core==3.12.0
ENV PYTHONPATH=/workspace/src:/workspace PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
WORKDIR /workspace
