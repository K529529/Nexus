FROM python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
RUN apt-get update && apt-get install -y --no-install-recommends git procps && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple pytest==8.4.2 setuptools==82.0.1 setuptools-scm==10.1.2 iniconfig==2.1.0 packaging==25.0 pluggy==1.6.0 pygments==2.19.2 attrs==25.3.0 hypothesis==6.140.2 numpy==2.3.3 pexpect==4.9.0
ENV PYTHONPATH=/workspace/src:/workspace PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
WORKDIR /workspace
