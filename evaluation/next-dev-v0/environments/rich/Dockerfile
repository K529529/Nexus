FROM python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
RUN apt-get update && apt-get install -y --no-install-recommends git procps && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple pytest==7.4.4 pygments==2.19.2 markdown-it-py==4.0.0 attrs==25.3.0
ENV PYTHONPATH=/workspace/src:/workspace PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
WORKDIR /workspace
