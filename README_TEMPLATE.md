# README template

The canonical section order and contents for `README.md`. Keep `README.md` in this
shape so releases and feature additions land in predictable places. Sections are
listed top-to-bottom in the order they must appear. **Required** sections always
exist; **Optional** sections appear only when relevant (and keep their slot in the
order when they do).

Separate every top-level section with a `---` horizontal rule.

---

## Section order at a glance

| # | Section | Heading | Required? |
|---|---|---|---|
| 1 | Title, badges & elevator pitch | `# <Name>` | Required |
| 2 | Why this exists | `## Why this exists` | Required |
| 3 | What's new | `## 🆕 What's new in vX.Y.Z` | Required |
| 4 | Prerequisites | `## Prerequisites` | Required |
| 5 | Installation | `## Installation` | Required |
| 6 | Configuration | `## Configuration` | Required |
| 7 | How it works | `## How it works` | Required |
| 8 | Entities | `## Entities` | Required |
| 9 | Services | `## Services` | Required |
| 10 | Use cases | `## Use cases` | Required |
| 11 | Automation examples | `## Automation examples` | Required |
| 12 | Known limitations | `## Known limitations` | Required |
| 13 | Troubleshooting | `## Troubleshooting` | Required |
| 14 | Standalone tools | `## Standalone tools` | Optional |
| 15 | Roadmap | `## Roadmap` | Optional |
| 16 | Compatibility | `## Compatibility` | Required |
| 17 | Home Assistant quality scale | `## Home Assistant quality scale` | Required |
| 18 | License | `## License` | Required |

Sections 10–13 are what the quality scale's Gold documentation rules ask for (`docs-use-cases`,
`docs-examples`, `docs-known-limitations`, `docs-troubleshooting`), so they are required here.

---

## 1. Title, badges & elevator pitch — *Required*

- `# <Integration name>` as the H1.
- **Badge block**, two rows, in this order:
  1. Status/meta shields (`style=for-the-badge`): Marketplace (links to the install steps), GitHub Release, downloads,
     license, commit activity, maintenance year, and the HA quality scale badge (links to
     `#home-assistant-quality-scale`; it says "no tier claimed" while no tier is claimed).
  2. Workflow status badges (Tests, Validate, hassfest, Security) — add each one when its
     workflow exists under `.github/workflows/`.
- One-sentence **value proposition** naming the portal it reads from.
- An **`It gives you:`** bulleted feature list — each bullet **bold lead-in** + plain-English
  benefit (Energy dashboard, Daily sensors, One login, Late data handled, No add-ons…).
- A closing **call-out** for the headline differentiator (e.g. "No Browserless, no Chrome").

## 2. Why this exists — *Required*

The motivation / problem statement. Two or three short paragraphs: what the portal does and
doesn't offer, what changed (mandatory one-time codes broke the browser-driven 1.x), and why
the plain-HTTP approach is better. No tables, no config detail.

## 3. What's new in vX.Y.Z — *Required*

- Heading carries the **current release version** and the 🆕 emoji.
- Bold-lead-in bullets in plain language describing **this** release's user-facing changes.
- Include an **"Upgrading?"** / **"Affected?"** bullet when behaviour or migration affects existing users,
  and a **"Nothing changes"** bullet saying who is unaffected.
- Below them, `From vX.Y.Z:` blocks keep short summaries of recent earlier releases, newest first, so
  someone skipping versions sees what they missed. Drop the oldest once they stop being useful.
- Close with links: `[CHANGELOG](CHANGELOG.md)` · `[release notes](…/releases/tag/vX.Y.Z)`.
- **Update this every release** — version in the heading, body, and the release-notes link
  must all match the new tag.

## 4. Prerequisites — *Required*

Numbered `### N. <topic>` subsections covering everything needed before install, in
dependency order. For this project:

1. **Portal account** — a working my.southeastwater.com.au login with a digital meter, and
   access to the email address or phone the portal sends one-time codes to.
2. **Recorder** — long-term statistics need the recorder (on by default); note that
   history is kept in the recorder database, not in this integration.
3. **Energy dashboard** (optional) — only if the user wants the water card.

Use `<details><summary>…</summary>` for long optional snippets so the section stays scannable.

## 5. Installation — *Required*

- Dashboard screenshot up top:

  ```markdown
  ![South East Water sensors in Home Assistant](images/dashboard.png)
  ```

- `### Marketplace (recommended, Home Assistant 2026.11 or newer)` numbered steps (Add from link; the
  repository isn't in the catalog), the 48-hour update check, and a note that HACS installs move over on
  their own.
- `### HACS (Home Assistant 2026.10 or older)` numbered steps.
- `### Manual` numbered steps.
- `### Removing the integration` — what is deleted and what is deliberately left behind
  (the `sew_water:water_usage_mains` statistic).
- A closing note on runtime deps (aiohttp ships with HA; nothing to install).

## 6. Configuration — *Required*

- Entry-point sentence (Settings → Devices & Services → Add Integration → …).
- A line stating how many wizard steps there are and which are conditional.
- One `### Step N — <name>` subsection per wizard step. **Each step leads with a
  screenshot of that wizard page**, then a **field table**
  (`| Field | Default | Description |`, drop the Default column where it doesn't apply).
- Mark conditional steps in the heading (e.g. "(only if the portal asks for a code)",
  "(only if your login holds more than one property)").
- `### Options` for the options flow, same field-table shape.
- `### Re-authentication` describing the reauth card and its steps.
- `### Reconfigure` describing what it replaces and what it keeps (entities, history, the chosen property).
- End with any operational ⚠️/heads-up call-outs.

Keep step numbers and field names in lockstep with `config_flow.py`/`strings.json`.

Per-step screenshot placeholders — one image per wizard step, named `config-stepN-<slug>.png`
under `images/`. Swap each placeholder for a real screenshot of that page:

```markdown
### Step 1 — Sign in
![Step 1 — Sign in](images/config-step1-signin.png)
<!-- placeholder: capture the credentials page -->

### Step 2 — Send code by
![Step 2 — Send code by](images/config-step2-channel.png)
<!-- placeholder: capture the Email / SMS chooser -->

### Step 3 — One-time code
![Step 3 — One-time code](images/config-step3-code.png)
<!-- placeholder: capture the code entry page -->

### Step 4 — Choose the property
![Step 4 — Choose the property](images/config-step4-property.png)
<!-- placeholder: capture the property drop-down (a login with two properties); blur the addresses -->

### Step 5 — Import history
![Step 5 — Import history](images/config-step5-history.png)
<!-- placeholder: capture the installation-date page -->
```

Screenshots must not show personal data: blur addresses, email addresses and account numbers.

## 7. How it works — *Required*

- Bulleted explanation of each core behaviour (session reuse, trailing re-import, statistics
  vs sensors, 02:00 poll), in plain language with the *why*, not code detail.
- Link out to `DESIGN_DOCUMENT.md` for the protocol and statistics design.
- `### Energy dashboard` subsection: which statistic to add and why not the sensor.

## 8. Entities — *Required*

Single table `| Sensor | Unit | Description |` listing every sensor the integration
creates, then the **Update now** button. Note diagnostic-only entities, any disabled by default, and
any that only exist on some properties (the recycled-water sensors). Keep in sync with `sensor.py`
and `button.py`.

## 9. Services — *Required*

Single table `| Service | Description |` of every registered service
(`<domain>.<service>`). Keep in sync with `services.yaml` / `__init__.py`.

## 10. Use cases — *Required*

Bold-lead-in bullets of what people actually do with the data (Energy dashboard, leak alerts,
consumption targets, long-term records), each one sentence on how.

## 11. Automation examples — *Required*

A bold one-line description of each example followed by a fenced `yaml` block that works as pasted
(real entity IDs, no placeholders the user must spot). Keep sensor names in sync with section 8.

## 12. Known limitations — *Required*

Bold-lead-in bullets of what the integration can't do and why (data latency, hourly resolution,
one login and one property, what the portal does not expose). Say what the user can do about each
where there is something.

## 13. Troubleshooting — *Required*

`| Symptom | What it means | What to do |` table for the common failures, followed by how to turn on
debug logging and what the log and **diagnostics** do and don't contain, so users can attach them to an
issue safely.

## 14. Standalone tools — *Optional*

Present only while the repository ships user-facing CLIs. Describe each tool, show a fenced
`bash` usage example, and note requirements. Use `### <tool>` subsections when there is more
than one.

## 15. Roadmap — *Optional*

Short bulleted list of planned/in-progress work with status, linking
`DESIGN_DOCUMENT.md#9-open-items` for the full list. Drop the section if there's nothing
public to promise.

## 16. Compatibility — *Required*

`| Component | Version |` table: Home Assistant min version, Python, and any
key runtime dep. Keep versions aligned with `manifest.json` / `hacs.json`.

## 17. Home Assistant quality scale — *Required*

What the quality scale is, that a custom integration can only self-assess, the tier claimed (or why
none is), and one bold-lead-in paragraph per tier summarising which rules pass, which are open and
which don't apply. Link `custom_components/sew_water/quality_scale.yaml` for the rule-by-rule record
and keep the two in sync. Use a `> [!NOTE]` block for any rule that blocks a tier.

## 18. License — *Required*

One line naming the license and linking `LICENSE`.

---

## Conventions

- **Horizontal rules** (`---`) separate every top-level (`##`) section.
- **Tables** for any field/sensor/service/version reference list; prose for concepts.
- **Bold lead-ins** on feature and explanation bullets.
- **Call-outs**: `> ⚠️` for warnings, `> **Heads up:**`/`> **Note:**` for advisories.
- **Collapsible `<details>`** for long optional snippets.
- **Images** live in `images/`: `dashboard.png` for the hero shot and
  `config-stepN-<slug>.png` for each wizard step. Every wizard step carries a screenshot.
- **Plain language first** — defer protocol and internals to `DESIGN_DOCUMENT.md`.
- **Release sync**: on every release update the version-bearing bits (badges resolve
  automatically; the "What's new" heading/body/link do not) and re-check the Entities,
  Services, Configuration, Compatibility and quality-scale sections against the code.
