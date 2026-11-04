# Linux leg of tools/release-check.py (DD-v3-tests § 3.2).
# Python at the kit's 3.11 floor (AGENTS.md), pinned by digest; the image tag is
# derived from this file's sha256, so any edit here builds a new image.
# python:3.11-slim, Python 3.11.18, pulled 2026-11-04.
FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893

# git: engine tests and the preflight read history; bash: test-adapter.sh.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git bash \
    && rm -rf /var/lib/apt/lists/*
