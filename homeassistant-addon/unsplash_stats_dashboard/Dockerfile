ARG BUILD_FROM=ghcr.io/astral-sh/uv:python3.12-bookworm-slim
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG BUILD_DESCRIPTION
ARG BUILD_NAME
ARG BUILD_VERSION

LABEL \
  io.hass.type="addon" \
  io.hass.arch="${BUILD_ARCH}" \
  io.hass.name="${BUILD_NAME}" \
  io.hass.description="${BUILD_DESCRIPTION}" \
  io.hass.version="${BUILD_VERSION}"

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN uv pip install --system \
    "dash>=2.18.2" \
    "gunicorn>=23.0.0" \
    "pandas>=2.2.3" \
    "plotly>=5.24.1"

COPY unsplash_stats /app/unsplash_stats
COPY run.sh /run.sh

RUN chmod a+x /run.sh

EXPOSE 8099

CMD ["/run.sh"]

