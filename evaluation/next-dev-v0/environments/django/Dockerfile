# Rebuild the task Python 3.6 environment after the original image pull timed out.
FROM swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1707@sha256:3c103594eb39c0564cfd71f12ecca0d75a9a4a821f33c6e43b036d2271e75de7
RUN rm -rf /testbed /root/setup_repo.sh /root/setup_env.sh /root/.cache /root/.bash_history /tmp/*
RUN /opt/miniconda3/bin/conda create -y -p /opt/eval --override-channels -c https://repo.anaconda.com/pkgs/main python=3.6.13 pip=21.2.2 setuptools=58.0.4 wheel=0.37.1
RUN /opt/eval/bin/python -m pip install --timeout 90 --no-cache-dir --index-url https://pypi.org/simple asgiref==3.4.1 pytz==2024.2 sqlparse==0.4.4 tblib==1.7.0
ENV PATH=/opt/eval/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH=/workspace
WORKDIR /workspace
