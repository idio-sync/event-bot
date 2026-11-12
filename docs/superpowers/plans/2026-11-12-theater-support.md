# Theatre support implementation plan

Goal: support SNF Parkway, Cinemark Towson and Warehouse Rotunda through existing Showing adapters.
Architecture: source fetch results explicitly state schedule completeness; only complete results deactivate missing listings. Use an isolated standard-library HTTP transport for Cinemark and Warehouse, with timeouts and async thread offload. Preserve HTTPX for other integrations.

1. Add regression tests for complete, partial and empty snapshots. Introduce FetchResult and wire refresh/database reconciliation; sanitize source errors. Preserve legacy full snapshots for RTS.
2. Test and implement transport, Cinemark configurable horizon and normalized URLs, configurable Parkway bucket and header authentication. Validate source IDs and timezone information. Verify Eventive pagination shape against official docs; refuse ambiguous/truncated responses.
3. Test and implement Warehouse structured-data parsing, film discovery, stable IDs, and partial snapshot results. Use bounded requests to movie pages, fail safely on malformed structured data, and report coverage limitations.
4. Register Warehouse and configuration, update README/design/env/Unraid settings, and test UI/Discord integration using existing registry.
5. Run full tests, live read-only source probes, review changes independently and correct findings. Do not contact theatres, send Discord alerts, deploy, push, or modify live data.

Validation: pytest full suite with temporary database fixtures; mocked fetch failures must preserve listings and redact credentials. Live probes must exercise adapters without running refresh or integrations. Deployment-network reliability remains a documented limitation.

Implementation decision: completeness is a conservative boolean, not a date-window cancellation mechanism. Parkway, Cinemark and Warehouse report partial coverage until a full source contract is verified. This retains potentially canceled shows until they pass; Status and README explain this tradeoff.

Follow-up approved: discover the Parkway browser key from its official homepage on every refresh when the environment override is blank. Keep keys in memory, authenticate through headers, fail safely for missing or ambiguous discovery, and test rotation/override behavior. User authorized committing all theatre-support changes after tests and review.
