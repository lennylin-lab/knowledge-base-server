# ES_VERSION must match the `ES_VERSION` build arg in docker-compose.yml — the
# analysis-ik artifact is version-locked to Elasticsearch.
ARG ES_VERSION
FROM docker.elastic.co/elasticsearch/elasticsearch:${ES_VERSION}

# The plugin zip is fetched into this directory before building (see README):
#   curl -fL -o docker/elasticsearch/elasticsearch-analysis-ik-${ES_VERSION}.zip \
#     https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-${ES_VERSION}.zip
# Installing from the local file instead of a URL keeps builds hermetic —
# infini's mirrors can throttle to KB/s, and a URL install would re-download
# on every cache-busting Dockerfile change. Alternative distribution channel:
# https://get.infini.cloud/elasticsearch/analysis-ik/${ES_VERSION}
# (the GitHub repo publishes no version-tagged release assets).
COPY elasticsearch-analysis-ik-*.zip /tmp/analysis-ik.zip
RUN elasticsearch-plugin install --batch "file:///tmp/analysis-ik.zip"
