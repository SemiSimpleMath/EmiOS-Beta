# MCP registry sources

This directory is for **upstream registry inputs** and pinned snapshots.

## Why we keep this

- MCP servers at scale need an index/registry to discover *what exists* and *how to run/connect*.
- The **official MCP Registry** provides standardized `server.json` metadata and an OpenAPI-based REST API.
- We snapshot upstream data so we can:
  - pin a known-good view of the ecosystem
  - diff changes over time
  - avoid surprise breakages from upstream churn

## Upstream references

- Official registry overview: `https://modelcontextprotocol.io/registry/about`

