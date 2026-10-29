# Technology rules

Linux server; Python 3.12+; Tencent qq-botpy with sandbox gateway and aiohttp.
Top-level code is a script-based distribution; install requirements.txt in a virtual environment.
Checks: python -m unittest discover -v; python -m compileall -q *.py.
Never alter a running deployment while preparing a public release. Do not commit live data.
