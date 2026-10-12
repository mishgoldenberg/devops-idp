# Wheels for the closed network

Python packages the build needs that the internal PyPI repository may not have yet.
The pipeline uploads each one it lacks (`scripts/upload_wheels.py`), and both
`pip install` steps also read this folder (`--find-links`), so a wheel that could not be
uploaded is still installed. Once a build shows them in the repository, a round deletes
the wheels; this file stays so the folder always exists.
