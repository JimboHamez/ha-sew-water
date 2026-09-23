# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Home Assistant (HA) Integration Development Standards

## 1. Context7 Documentation Rules
- Always use the Context7 MCP server to fetch version-accurate API references and code snippets before generating or modifying code that uses external libraries. Do not rely on base training data for fast-evolving frameworks.
- If asked to implement or modify features using frameworks like Home Assistant Core or auxiliary dependencies, always precede your response by invoking the Context7 tools.
- Append "use context7" to your planning steps if you need to research the latest documentation.
- Before writing or editing any Python file under `custom_components/sew_water/`, query Context7 for each Home Assistant API the change touches, unless that API was already checked in this session. A `PreToolUse` hook in `.claude/settings.local.json` fires on `Edit`/`Write` of those files as a reminder.
- The server is defined in a local `.mcp.json` at the repo root. The file is git-ignored on purpose and never committed, and is enabled through `enabledMcpjsonServers` in `.claude/settings.local.json`. It reads its key from the `CONTEXT7_API_KEY` environment variable. Never write the key into the file.
- Known library IDs. Pass these straight to `query-docs` and skip `resolve-library-id`:
  - `/home-assistant/developers.home-assistant`: the HA developer docs, covering integrations, `DataUpdateCoordinator`, config/options flows, entities, the quality scale and translations. Use this one first.
  - `/home-assistant/core`: HA Core source, for exact signatures and behaviour.
  - `/home-assistant/home-assistant.io`: user-facing docs, for YAML/UI behaviour, recorder, statistics and energy.

## 2. Architectural Guardrails
- **The Async Iron Law:** Never allow blocking code (e.g., `requests`, `time.sleep`, or synchronous file reads) in the main thread. Always wrap synchronous device calls in `await hass.async_add_executor_job()` or rewrite them natively using `aiohttp` or `asyncio`.
- **Data Coordination:** Always scaffold the integration using a central `DataUpdateCoordinator`. Individual entities must inherit from `CoordinatorEntity` and pull states from the coordinator's cached data, rather than querying the API directly to prevent rate-limiting.
- **UI-Driven Configuration:** Do not write YAML parsing routines. Generate UI-driven `ConfigFlow` components (`config_flow.py`) for initial setup and an `OptionsFlow` for changing parameters later without restarting Home Assistant.
- **Client Library Separation:** All raw API-specific network code, parsing, and authentication handling must live in a separate third-party Python client library (declared in the `manifest.json` `requirements` array). The custom component code should only orchestrate state translation.
- **HACS Layout Compliance:** Ensure the repository follows HACS layout standards. The `manifest.json` must explicitly contain a valid `"version"` key and a `"codeowners"` list. Generate a `hacs.json` file automatically in the project root.

## 3. Python Coding & Style Guidelines
- **Formatting:** Code must pass Ruff styling defaults with a 120-character line limit. Run `ruff format` and `ruff check --fix` before completing any file modification.
- **Import Conventions:** Order imports strictly by standard library, third-party, and then local modules. Ensure constants and dictionary keys are sorted alphabetically. Adhere strictly to Home Assistant's mandatory custom framework shortcut module bindings (e.g., import `homeassistant.util.dt` as `dt_util`).
- **Type Checking:** Every function signature must be fully typed (arguments and return types). Prefer concrete types over `Any` (some idiomatic HA `Any` remains — config-flow `user_input`, voluptuous schema dicts, raw-JSON payloads). Must pass strict (`mypy`-compliant) analysis; use `assert` to narrow types when Core context is ambiguous. Import and use structural types from the core (`HomeAssistant`, `ConfigEntry`, `DiscoveryInfoType`). Include a `py.typed` file in the package root to satisfy PEP-561 compliance.
- **Documentation:** Public methods must use Google-style docstrings. Comments must be complete sentences ending in a period.
- **Logging Restrictions:** Do not include the platform or domain name manually inside log strings (e.g., write `_LOGGER.error("Failed to connect")`, not `_LOGGER.error("[MyDomain] Failed to connect")`). Never log sensitive strings like API keys, tokens, or local passwords. Use `_LOGGER.debug` for developer diagnostics.
- **Native Constants:** Never hardcode states like `'on'`, `'off'`, `'unavailable'`, or metrics like `'C'`. Always import and use native constants from `homeassistant.const` (e.g., `STATE_ON`, `STATE_OFF`, `STATE_UNAVAILABLE`, `UnitOfTemperature.CELSIUS`).
- **Entity Naming:** Do not assign a raw string to the `_attr_name` property of an entity. Set `_attr_has_entity_name = True` and use localized device naming keys via translation strings inside the `strings.json` file.
- **Exception Handling:** Wrap external Python client calls in `homeassistant.exceptions.HomeAssistantError` variations (like `ConfigEntryNotReady`) to trigger safe auto-retries and elegant user-facing UI dialogs.
