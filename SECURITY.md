# Security policy

## Supported releases

USYNC is in alpha. No production release or guaranteed maintenance window is available yet.

## Reporting vulnerabilities

Please use GitHub's **Report a vulnerability** / private vulnerability reporting feature for this repository if enabled. If it is not available, open a minimal public issue requesting a private reporting channel **without disclosing exploit details, credentials, or personal information**.

## Sensitive information

USYNC can handle Canvas API tokens, Ed x-tokens, browser SSO cookies, personal grade/feedback records, downloaded material and login-related HAR files. Never commit or attach those files to issues or pull requests. Treat generated study ZIPs as private unless you have permission to share all their contents.

HTTP requests to file URLs from different hosts must not carry the Canvas or Ed authentication headers. All remote access must respect the service's access rules and applicable rate limits.
