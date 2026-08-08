# Security Policy

## Supported version

Only the latest `main` branch is supported.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when available. Do not attach real chat exports, tokens, group IDs, addresses, phone numbers, screenshots, or reports to a public issue.

For ordinary bugs, create a minimal reproduction using synthetic data. Remove all local paths and identifiers before posting logs.

## Local API safety

The monitor intentionally accepts only loopback WeFlow API addresses. Do not modify it to listen on or connect to a LAN/public address unless you fully understand the exposure of local chat data.
