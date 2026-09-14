# The image sandbox containers are built from.
#
#   docker build -t pi-sandbox:latest deploy/sandbox_image
#
# What this image is for: giving the agent a shell that can actually do work.  What it is
# NOT is a security boundary — the boundary is the container configuration (one bind mount,
# dropped capabilities, no docker socket, resource caps) applied by the provisioner.  A
# heavier image does not make the sandbox stronger; it only makes the agent more useful.
#
# Deliberate choices:
#
# * **No ENTRYPOINT.**  The provisioner starts the container with `sleep infinity` and runs
#   each command through `docker exec`.  An entrypoint here would replace that and the
#   container would exit immediately.
# * **No VOLUME declaration.**  The workspace mount is supplied at run time; baking one in
#   would create an anonymous volume that outlives the container and accumulates garbage.
# * **`HOME` is not set here.**  The provisioner sets `HOME=/workspace`, so a command that
#   writes dotfiles (git, pip, npm) lands in the bind-mounted workspace, not in a layer that
#   disappears with the container.
# * **git is configured to trust the workspace.**  Files in a bind mount are owned by the
#   host uid, which is not the uid inside the container unless the operator matched them;
#   without this, `git status` refuses to run at all.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        git \
        jq \
        make \
        less \
        unzip \
        ripgrep \
        fd-find \
        build-essential \
        pkg-config \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    # `fd` is packaged as fdfind on Debian; the agent looks for `fd`.
    && ln -sf "$(command -v fdfind)" /usr/local/bin/fd

# Node is not in Debian slim; most teams want it for front-end work. Remove if not needed —
# it is the single largest thing in this image.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# `timeout` (coreutils) is what the provisioner wraps every command in, and it is already
# present. Verify at build time so a future base image change fails loudly rather than at
# the first command a user runs.
RUN command -v timeout && command -v sh && command -v git

# Anything the agent writes outside /workspace is ephemeral by design: it lives in the
# container's writable layer and dies with the container. That includes `pip install`.
WORKDIR /workspace

CMD ["sleep", "infinity"]
