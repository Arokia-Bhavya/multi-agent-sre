"""
Multi-Agent AI SRE Platform.

Subpackages:
    tools       - Observability clients (Prometheus, Jaeger, Kubernetes,
                  Alertmanager) and the agents built on top of them.
    coordinator - LangGraph investigation graph: root-cause analysis and
                  remediation runbook generation.
    copilot     - Conversational ReAct agent (SRECopilot) and CLI.
    webui       - FastAPI backend + browser UI, including Alertmanager
                  webhook auto-triage.
"""
