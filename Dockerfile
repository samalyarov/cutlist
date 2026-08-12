FROM python:3.12-slim

# ffmpeg and ffprobe do all the media work. fonts-dejavu-core installs
# DejaVuSans-Bold at exactly the path caption.py falls back to on Linux, and
# DejaVu covers Cyrillic -- so captions work with no code change. libgl1 is for
# opencv: scenedetect drags in the non-headless build even though cutlist pins
# the headless one, and it will not import without libGL.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE /src/
COPY cutlist /src/cutlist
RUN pip install --no-cache-dir /src && rm -rf /src

# Non-root, so files written into a bind-mounted workspace are not root-owned
# on the host.
RUN useradd --create-home --uid 1000 cutlist
USER cutlist

# The workspace root. Mount yours here: docker run -v "$PWD:/work" ...
WORKDIR /work

ENTRYPOINT ["cutlist"]
CMD ["--help"]
