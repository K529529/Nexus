# Local rebuild after source-image pull timeout; task dependency versions derive from SWE-bench environment.yml.
FROM swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1707@sha256:3c103594eb39c0564cfd71f12ecca0d75a9a4a821f33c6e43b036d2271e75de7
RUN rm -rf /testbed /root/setup_repo.sh /root/setup_env.sh /root/.cache /root/.bash_history /tmp/*
RUN /opt/miniconda3/envs/testbed/bin/python -m venv /opt/eval
RUN /opt/eval/bin/python -m pip install --no-cache-dir --index-url https://pypi.org/simple pytest==7.4.4 pytest-mock==3.14.0 certifi==2024.8.30 charset-normalizer==2.0.12 idna==3.10 urllib3==1.26.20 chardet==4.0.0
ENV PATH=/opt/eval/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH=/workspace/src:/workspace PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
WORKDIR /workspace
ENV PYTEST_PLUGINS=pytest_mock
