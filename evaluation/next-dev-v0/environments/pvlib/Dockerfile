FROM swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1707@sha256:3c103594eb39c0564cfd71f12ecca0d75a9a4a821f33c6e43b036d2271e75de7
RUN rm -rf /testbed /root/setup_repo.sh /root/setup_env.sh /root/.cache /root/.bash_history /tmp/*
ENV PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH=/workspace/src:/workspace PYTEST_DISABLE_PLUGIN_AUTOLOAD=""
WORKDIR /workspace
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple numpy==1.26.4 pandas==1.5.3 scipy==1.13.1
