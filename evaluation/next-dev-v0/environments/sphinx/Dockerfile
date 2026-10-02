# Local rebuild after source-image pull timeout; task dependency versions derive from SWE-bench environment.yml.
FROM swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1707@sha256:3c103594eb39c0564cfd71f12ecca0d75a9a4a821f33c6e43b036d2271e75de7
RUN rm -rf /testbed /root/setup_repo.sh /root/setup_env.sh /root/.cache /root/.bash_history /tmp/*
RUN /opt/miniconda3/envs/testbed/bin/python -m venv /opt/eval
RUN /opt/eval/bin/python -m pip install --timeout 90 --retries 3 --no-cache-dir --index-url https://pypi.org/simple pytest==8.3.3 alabaster==0.7.16 babel==2.16.0 docutils==0.17.1 imagesize==1.4.1 jinja2==3.1.4 markupsafe==3.0.2 packaging==24.1 pygments==2.18.0 requests==2.32.3 snowballstemmer==2.2.0 html5lib==1.1 sphinxcontrib-applehelp==1.0.2 sphinxcontrib-devhelp==1.0.2 sphinxcontrib-htmlhelp==2.0.0 sphinxcontrib-jsmath==1.0.1 sphinxcontrib-qthelp==1.0.3 sphinxcontrib-serializinghtml==1.1.5
ENV PATH=/opt/eval/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH=/workspace/src:/workspace PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
WORKDIR /workspace
