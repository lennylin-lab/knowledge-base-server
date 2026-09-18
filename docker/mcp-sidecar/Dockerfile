# syntax=docker/dockerfile:1

# One stdio MCP server behind supergateway's streamable-HTTP transport, so the
# Python API container never needs Node or subprocess supervision. The bridge
# and the target server are installed (pinned) at build time — the container
# does no npm downloads at run time. Build again with different args to bridge
# a different stdio server; usage lives in docs/mcp-sidecars.md.

ARG NODE_VERSION=22-bookworm-slim
FROM node:${NODE_VERSION}

ARG SUPERGATEWAY_VERSION=3.4.3
ARG MCP_PACKAGE=@upstash/context7-mcp
ARG MCP_VERSION=4.1.1
RUN npm install -g --omit=dev \
    "supergateway@${SUPERGATEWAY_VERSION}" \
    "${MCP_PACKAGE}@${MCP_VERSION}"

# MCP_COMMAND is the npm bin name of MCP_PACKAGE; MCP_ARGS carries constant
# arguments for it (quoted once, expanded below). Override per sidecar via
# compose environment; versions only via build args.
ENV MCP_COMMAND=context7-mcp \
    MCP_ARGS="" \
    GATEWAY_PORT=9300

EXPOSE 9300

# Any HTTP response from /mcp proves the listener is alive — a bare GET is
# rejected with 4xx by the streamable-HTTP endpoint, which still counts.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:' + process.env.GATEWAY_PORT + '/mcp').then(() => process.exit(0)).catch(() => process.exit(1))"

USER nobody

# Stateless mode: each client request re-initializes the stdio server, so no
# gateway-side session store can expire under the API's long-lived connection.
CMD supergateway \
    --stdio "${MCP_COMMAND}${MCP_ARGS:+ ${MCP_ARGS}}" \
    --outputTransport streamableHttp \
    --streamableHttpPath /mcp \
    --port "${GATEWAY_PORT}"
