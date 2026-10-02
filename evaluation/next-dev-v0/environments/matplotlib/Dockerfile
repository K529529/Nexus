# Build extensions from the exact task base; no upstream fix or hidden tests enter this image.
FROM swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1707@sha256:3c103594eb39c0564cfd71f12ecca0d75a9a4a821f33c6e43b036d2271e75de7
RUN rm -rf /testbed /root/setup_repo.sh /root/setup_env.sh /root/.cache /root/.bash_history /tmp/*
RUN apt-get update && apt-get install -y --no-install-recommends ghostscript && rm -rf /var/lib/apt/lists/*
RUN /opt/miniconda3/envs/testbed/bin/python -m venv /opt/eval
RUN /opt/eval/bin/python -m pip install --timeout 90 --no-cache-dir --index-url https://pypi.org/simple numpy==1.23.5 pillow==9.5.0 setuptools==59.8.0 wheel==0.44.0 pytest==7.4.4 certifi==2024.8.30 packaging==24.1 python-dateutil==2.9.0.post0 cycler==0.11.0 fonttools==4.39.4 kiwisolver==1.4.7 pyparsing==3.0.9
ENV PATH=/opt/eval/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY source.tar.gz /tmp/source.tar.gz
RUN mkdir /tmp/source && tar -xf /tmp/source.tar.gz -C /tmp/source --strip-components=1 && cd /tmp/source && python setup.py build_ext --inplace -j 1 && mkdir -p /opt/nexus-generated && find lib -name '*.so' -exec cp --parents '{}' /opt/nexus-generated/ \; && cp lib/matplotlib/_version.py /opt/nexus-generated/lib/matplotlib/ && rm -rf /tmp/source /tmp/source.tar.gz /root/.cache
ENV PYTHONPATH=/workspace/lib:/workspace PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 MPLBACKEND=Agg
WORKDIR /workspace
RUN python -m pip install --timeout 90 --no-cache-dir --index-url https://pypi.org/simple setuptools-scm==6.4.2
ENV SETUPTOOLS_SCM_PRETEND_VERSION=3.6.0.dev0
