# Knowledge Base

Human-authored playbooks for known failure patterns, searched by
`ai_platform/tools/knowledge_base.py`'s `search_knowledge_base` (exposed to
the copilot as a tool of the same name). Distinct from the platform's
incident history (`incident_search.py`'s `search_similar_incidents`, which
searches *auto-generated* RCA reports from past alerts): these are docs a
human wrote once, that apply every time a matching failure recurs, whether
or not it's fired before.

## Format

- One `*.md` file per playbook, directly under this directory (not
  recursive — subfolders aren't scanned).
- Start with a top-level `# Title` heading — it's what shows up in search
  results and copilot answers. Files without one just fall back to their
  filename.
- Whole-file granularity: each file is indexed and matched as a single
  document, not split by section. Keep each playbook focused on one
  failure pattern (a few hundred words) rather than one giant doc covering
  everything — that keeps a match specific and keeps a diagnostic step
  from getting separated from the symptom description that motivates it.
- `README.md` (this file) is excluded from indexing by name.

## Adding a new playbook

Drop a new `.md` file here — no registration step, no index to rebuild by
hand. `search_knowledge_base` reloads every file in this directory fresh
on every call, so a new or edited file is picked up on its very next
search.

## The starter set

The four playbooks currently here (`high-error-rate.md`,
`high-latency.md`, `pod-down.md`, `high-memory-usage.md`) match the
failure patterns behind the alert rules defined in
`observability/helm/otel-demo-minimal-values.yaml` — each rule fires per
service (`frontend`, `product-catalog`) but shares one underlying pattern,
so one playbook per pattern covers both rather than duplicating near-
identical docs per service.
